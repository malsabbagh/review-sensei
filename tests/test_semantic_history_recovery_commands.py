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
from types import SimpleNamespace
from unittest.mock import patch

from review_sensei_github import action_run, review_run
from review_sensei_github import cli as package_cli
from review_sensei_github.accepted_semantic_history import (
    _STATE_ADVANCING_COMMAND_ACTIONS,
    LIMIT,
    _target_allows_disposition_entry,
    guard_carried_successor,
    prepare_action,
)
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

from review_sensei.errors import ReviewInputError

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
    def test_target_allows_disposition_entry_only_for_state_advancing_commands(self):
        base = "a" * 40
        head = "b" * 40
        for action in sorted(_STATE_ADVANCING_COMMAND_ACTIONS):
            caller = SimpleNamespace(
                _accepted_semantic_target=("command", base, head, action)
            )
            self.assertTrue(_target_allows_disposition_entry(caller), action)

        for target in (
            None,
            (),
            ("command", base, head),
            ("command", base, head, None),
            ("command", base, head, ""),
            ("command", base, head, "status"),
            ("command", base, head, "pause"),
            ("command", base, head, "verify"),
            ("command", base, head, "unknown"),
            ("reply", base, head, "dismiss"),
            ("reply", base, head, None),
            ("review", base, head, "dismiss"),
            ["command", base, head, "dismiss"],
        ):
            caller = SimpleNamespace(_accepted_semantic_target=target)
            self.assertFalse(_target_allows_disposition_entry(caller), repr(target))

    def test_bootstrap_rejects_invalid_command_action_target(self):
        from argparse import Namespace

        for purpose, action in (
            ("review", "dismiss"),
            ("reply", "dismiss"),
            ("command", ""),
            ("command", 123),
        ):
            with self.subTest(purpose=purpose, action=action):
                with self.assertRaisesRegex(ReviewInputError, "semantic target"):
                    review_run.bootstrap(
                        Namespace(diff=Path("unused.diff")),
                        run_directory=Path("unused"),
                        policy_commit="a" * 40,
                        purpose=purpose,
                        command_action=action,
                    )

    def test_run_action_classifies_all_command_routes_and_passes_command_action(self):
        fp = "e" * 64
        scope = "a" * 64
        cases = (
            ("command", f'@reviewsensei dismiss {fp} --reason "safe"', "dismiss"),
            ("command", f'@reviewsensei defer {fp} --reason "later"', "defer"),
            (
                "command",
                f'@reviewsensei accept-risk {fp} --reason "mitigated"',
                "accept-risk",
            ),
            ("command", "@reviewsensei override RS-ABCDEF", "accept-risk"),
            (
                "command",
                "@reviewsensei override RS-ABCDEF acceptable risk",
                "accept-risk",
            ),
            ("command", "@reviewsensei RS-ABCDEF is acceptable risk", "accept-risk"),
            (
                "command",
                '@reviewsensei accept-coverage-uncertainty --reason "audited manually"',
                "accept-coverage-uncertainty",
            ),
            (
                "command",
                "@reviewsensei i reviewed the media files and i approve",
                "approve-media",
            ),
            ("command", "@reviewsensei review continue", "continue"),
            ("command", "@reviewsensei review reenroll", "reenroll"),
            (
                "command",
                f'@reviewsensei dismiss {fp} --reason "historical:{scope} Validated safe historical concern."',
                "dismiss",
            ),
            (
                "command",
                f'@reviewsensei accept-risk {fp} --reason "quarantine:{scope} Validated acceptable carried risk."',
                "accept-risk",
            ),
            ("command", "@reviewsensei review status", "status"),
            ("command", "@reviewsensei review pause", "pause"),
            ("command", "@reviewsensei verify", "verify"),
            ("command", "@reviewsensei unrecognized prose", None),
            ("reply", None, None),
        )
        for purpose, body, expected_action in cases:
            with (
                self.subTest(purpose=purpose, body=body),
                tempfile.TemporaryDirectory() as directory,
            ):
                captured = []
                sentinel = RuntimeError("stop-after-bootstrap")

                def fake_bootstrap(args, **kwargs):
                    captured.append((args, kwargs))
                    raise sentinel

                argv = [
                    "--policy-commit",
                    "a" * 40,
                    "--run-directory",
                    directory,
                    "--",
                    "--repository",
                    "owner/repo",
                    "--repository-id",
                    "123",
                    "--pull-request",
                    "7",
                    "--head-sha",
                    "b" * 40,
                    "--source-comment-id",
                    "41",
                    "--allow-write",
                ]
                if purpose == "command":
                    argv.extend(
                        [
                            "--comment-body",
                            body,
                            "--actor",
                            "alice",
                            "--association",
                            "OWNER",
                            "--github-session-ledger",
                        ]
                    )
                else:
                    argv.extend(
                        [
                            "--source-updated-at",
                            "2026-10-26T00:00:00Z",
                            "--provider",
                            "cloud-ollama",
                        ]
                    )

                with (
                    patch.object(
                        action_run,
                        "event_source",
                        return_value={"kind": "issue_comment", "id": 41},
                    ),
                    patch.object(action_run, "bootstrap", side_effect=fake_bootstrap),
                    self.assertRaisesIs(RuntimeError, sentinel),
                ):
                    action_run.run_action(argv, purpose=purpose)

                self.assertEqual(len(captured), 1)
                self.assertEqual(captured[0][1]["command_action"], expected_action)

    def test_execute_command_security_fence_rejects_mismatched_action_or_source(self):
        from tests.test_trust_status_route import StatusFixture

        for fault in ("action_mismatch", "body_mismatch"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                f = StatusFixture(directory)
                f.caller._accepted_semantic_target = (
                    "command",
                    "a" * 40,
                    HEAD,
                    "dismiss",
                )
                if fault == "body_mismatch":
                    f.args.comment_body = (
                        f'@reviewsensei dismiss {"e" * 64} --reason "tampered"'
                    )
                with (
                    patch.object(
                        f.caller,
                        "admit_action",
                        side_effect=AssertionError("admit_action must not run"),
                    ),
                    self.assertRaises(ReviewInputError),
                ):
                    action_run.execute_command(f.run, f.args)

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
            reply_fx = RecoveryEntryFixture(tempfile.mkdtemp(dir=directory))
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

            f.SOURCE_ID += 1
            f.select_command("@reviewsensei verify", new_run=True)
            self.assertEqual(f.invoke_command(), 1)
            self.assertIn("accepted_semantic_history_finite_no_progress_stop", f.error)

    def test_forged_or_tampered_history_still_fails_even_for_disposition_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            f, record, history = _seed_two_status_streak(directory)
            caller = SimpleNamespace(
                _accepted_semantic_target=("command", "a" * 40, HEAD, "dismiss"),
                _root=record.record_sha256,
            )
            tampered_streak = copy.deepcopy(history)
            tampered_streak["streak"] = 0  # inconsistent with records[-2:]
            with self.assertRaisesRegex(ReviewInputError, "inconsistent_streak"):
                guard_carried_successor(
                    caller, f.selected.ledger, IDENTITY, record, tampered_streak
                )

            tampered_scope = copy.deepcopy(history)
            tampered_scope["repository_id"] = 999999
            with self.assertRaisesRegex(ReviewInputError, "foreign_scope"):
                guard_carried_successor(
                    caller, f.selected.ledger, IDENTITY, record, tampered_scope
                )

            # Also verify _prepare_action refuses forged predecessor history.
            caller._accepted_semantic_predecessor = tampered_streak
            fake_run = SimpleNamespace(
                caller=caller,
                ledger=f.selected.ledger,
                identity=IDENTITY,
                base_sha="a" * 40,
            )
            with self.assertRaisesRegex(ReviewInputError, "inconsistent_streak"):
                prepare_action(fake_run, allow_unqualified=True)

            # End-to-end tampered history returned for the retained action result
            # still blocks dismiss in guard_carried_successor before rollover.
            index = parse_finding_index(
                f.reviews[-1]["body"],
                repository_id=123,
                pull_request=7,
                head_sha=HEAD,
            )
            assert index is not None
            instance = index.entries[0].instance
            saved = f.comment["body"]
            from review_sensei_github import api_run_history

            real_read = api_run_history.read_action_result

            def forged_read(ledger, identity, rec):
                retained = copy.deepcopy(real_read(ledger, identity, rec))
                retained["result"]["accepted_semantic_history"]["streak"] = 0
                return retained

            f.SOURCE_ID += 1
            f.select_command(
                f'@reviewsensei dismiss {instance} --reason "Should reject forged history."',
                new_run=True,
            )
            with patch.object(
                api_run_history, "read_action_result", side_effect=forged_read
            ):
                self.assertEqual(f.invoke_command(), 1)
            self.assertIn("accepted_semantic_history_inconsistent_streak", f.error)
            self.assertEqual(f.comment["body"], saved)

    def assertRaisesIs(self, exc_type, exc_instance):
        class _Ctx:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, etype, value, tb):
                self.assertIs(etype, exc_type)
                self.assertIs(value, exc_instance)
                return True

        return _Ctx()


if __name__ == "__main__":
    if len(sys.argv) == 3:
        _subprocess_phase(Path(sys.argv[1]), sys.argv[2])
    else:
        unittest.main()
