"""Tests for pre-rollover command syntax validation and opt-in G32 CLI cancellation."""

from __future__ import annotations

import copy
import hashlib
import inspect
import io
import json
import os
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit
from urllib.request import Request

from fake_github_http import json_response
from review_sensei_github import action_run, review_run
from review_sensei_github import cli as package_cli
from review_sensei_github.action_reply import execute_reply
from review_sensei_github.action_run import (
    cancel_unstarted_after_source_mutation,
    execute_command,
)
from review_sensei_github.api_run_history import read_action_result
from review_sensei_github.api_run_requests import ApiRunRequestTransport
from review_sensei_github.broker_client import BrokerClient
from review_sensei_github.conversation import ReplyResult
from review_sensei_github.errors import GitHubBrokerClientError
from review_sensei_github.http import GitHubHttp
from review_sensei_github.run_authority import parse_authority
from review_sensei_github.session_ledger import parse_session_comment
from test_action_reply import ReplyFixture
from test_action_reply_entry import GRANT, ReplyEntryFixture
from test_action_run import (
    CommandEntryFixture,
    historical_body,
    historical_command_case,
)
from test_api_work_lifecycle import forbid_replacement_phase_budget
from test_api_work_persistence import IDENTITY
from test_review_run import fixture_process_environment

from review_sensei.api_work import (
    ApiExecutionOwner,
    ApiRunAccounting,
    PendingApiRequest,
)
from review_sensei.errors import ReviewInputError


class _PreEffectCommandFixture(CommandEntryFixture):
    """CommandEntryFixture that supports toggling 404 after before_source_read."""

    def __init__(self, directory: str, *, limit: int = 1024) -> None:
        super().__init__(directory, limit=limit)
        self.deleted_source_ids: set[int] = set()
        self.past_run_attempts: dict[int, int] = {}

    def external(self, request, timeout):
        path = urlsplit(request.full_url).path
        method = request.get_method()
        if (
            self.command_active
            and method == "GET"
            and path == f"/repos/owner/repo/issues/comments/{self.SOURCE_ID}"
        ):
            self._meter(request)
            self.source_reads += 1
            if self.before_source_read:
                self.before_source_read(self)
            if self.SOURCE_ID in self.deleted_source_ids:
                return json_response({"message": "Not Found"}, status=404)
            return json_response(copy.deepcopy(self.source))
        response = super().external(request, timeout)
        if "/actions/runs/" in path and path.endswith("/jobs"):
            run_id = int(path.split("/actions/runs/")[1].split("/")[0])
            if run_id in self.past_jobs:
                value = json.loads(response.read(524288))
                value["jobs"][0]["name"] = "ReviewSensei / " + self.past_jobs[run_id]
                return json_response(value)
        return response


class _PreEffectReplyFixture(ReplyEntryFixture):
    """ReplyEntryFixture supporting mid-flight edit/404 and inline review comments."""

    def __init__(self, directory: str, *, limit: int = 1024) -> None:
        super().__init__(directory, limit=limit)
        self.source_reads = 0
        self.source_deleted = False
        self.source_status_override: int | None = None
        self.before_source_read = None
        self.past_jobs: dict[int, str] = {}
        self.source_kind_cli = "issue"
        self.extra_cli_flags: tuple[str, ...] = ()

    def prepare_reply(self, *, source_id: int = 41) -> None:
        super().prepare_reply(source_id=source_id)
        self.source_kind_cli = "issue"

    def prepare_inline_reply(self, *, source_id: int = 41) -> None:
        self.prepare_reply(source_id=source_id)
        self.event_name = "pull_request_review_comment"
        self.source_kind_cli = "inline"
        self.extra_comments.pop(source_id, None)
        self.inline.append(self.source)

    def external(self, request, timeout):
        path = urlsplit(request.full_url).path
        method = request.get_method()
        if method == "GET" and path in {
            f"/repos/owner/repo/issues/comments/{self.source['id']}",
            f"/repos/owner/repo/pulls/comments/{self.source['id']}",
        }:
            self._meter_inline_safe(request)
            self.source_reads += 1
            if self.before_source_read is not None:
                self.before_source_read(self)
            if self.source_status_override is not None:
                return json_response(
                    {"message": "Error"}, status=self.source_status_override
                )
            if self.source_deleted:
                return json_response({"message": "Not Found"}, status=404)
            return json_response(copy.deepcopy(self.source))
        if method == "GET" and path == "/repos/owner/repo/pulls/7/comments":
            self._meter_inline_safe(request)
            return json_response(copy.deepcopy(self.inline))
        response = super().external(request, timeout)
        if "/actions/runs/" in path and path.endswith("/jobs"):
            run_id = int(path.split("/actions/runs/")[1].split("/")[0])
            if run_id in self.past_jobs:
                value = json.loads(response.read(524288))
                value["jobs"][0]["name"] = "ReviewSensei / " + self.past_jobs[run_id]
                return json_response(value)
        return response

    def _meter_inline_safe(self, request) -> None:
        assert (
            len(self.accounted.observations)
            == sum(a == self.attempt for a, _, _ in self.trace) + 1
        )
        self.trace.append(
            (self.attempt, request.get_method(), urlsplit(request.full_url).path)
        )

    def broker_response(self, payload):
        if "session_attestation" in payload:
            parsed_preview = parse_authority(
                payload["session_attestation"], grant="session_grant" in payload
            )
            if parsed_preview.get("purpose") != "reply":
                return super(ReplyEntryFixture, self).broker_response(payload)
        if "session_grant" in payload:
            assert payload["session_grant"] == GRANT
            parsed = parse_authority(payload["session_attestation"], grant=True)
            assert parsed["purpose"] == "reply"
            return {
                "session_attestation": parsed,
                "control_receipt": {**payload["control_budget"], "dispatched": 1},
            }
        authority = parse_authority(payload["session_attestation"])
        assert authority["purpose"] == "reply"
        expected_kind = (
            "review_comment"
            if self.event_name == "pull_request_review_comment"
            else "issue_comment"
        )
        assert authority["run_id"] == str(self.run_id)
        assert authority["source"] == {
            "kind": expected_kind,
            "id": self.source["id"],
            "updated_at": self.source["updated_at"],
            "body_sha256": hashlib.sha256(self.source["body"].encode()).hexdigest(),
        }
        result = {
            "token": "synthetic-" + payload["capability"],
            "capability": payload["capability"],
            "control_receipt": {**payload["control_budget"], "dispatched": 1},
        }
        if payload["capability"] == "review_session":
            result.update(
                session_state="known" if self.comment else "enrolled",
                app_id=999,
                reservation_owner={
                    "run_id": str(self.run_id),
                    "head_sha": self.live_head,
                },
                session_grant=GRANT,
                session_attestation={
                    **authority,
                    "actor": {"login": "alice", "type": "User", "association": "OWNER"},
                },
            )
        return result

    def invoke_reply_cli(self, *, extra: tuple[str, ...] = ()) -> int:
        from review_sensei_github import action_reply

        fixture = self
        event = self.root / "reply-event.json"
        payload: dict[str, object] = {
            "action": "created",
            "comment": self.source,
            "repository": {"id": 123, "full_name": "owner/repo"},
            "sender": self.source["user"],
        }
        if self.event_name == "pull_request_review_comment":
            payload["pull_request"] = {"number": 7}
        else:
            payload["issue"] = {"number": 7, "pull_request": {}}
        event.write_text(json.dumps(payload), encoding="utf-8")
        environment = {
            **fixture_process_environment(),
            "GITHUB_RUN_ID": str(self.run_id),
            "GITHUB_RUN_ATTEMPT": str(self.attempt),
            "GITHUB_EVENT_NAME": self.event_name,
            "GITHUB_EVENT_PATH": str(event),
            "GITHUB_TOKEN": "synthetic-workflow-read",
            "GITHUB_ACTOR": "alice",
            "GITHUB_TRIGGERING_ACTOR": "alice",
            "REVIEW_SENSEI_WORKFLOW_REF": "malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@refs/tags/v5",
            "REVIEW_SENSEI_WORKFLOW_SHA": "a" * 40,
            "OLLAMA_API_KEY": "synthetic",
            "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "synthetic",
            "ACTIONS_ID_TOKEN_REQUEST_URL": "https://oidc.github.test/token?fixture=1",
        }

        def caller_factory(**kwargs):
            caller = ApiRunRequestTransport(**kwargs)
            fixture.accounted = caller
            fixture.transports.append(caller)
            return caller

        def broker_factory(**kwargs):
            return BrokerClient(
                broker_url="https://broker.github.test/api/github/token", **kwargs
            )

        def http_factory(**kwargs):
            http = GitHubHttp(api_url="https://api.github.test", **kwargs)
            fixture.http = http
            return http

        original_bootstrap = action_run.bootstrap
        bootstrap_code = original_bootstrap.__code__
        bootstrap_globals = original_bootstrap.__globals__

        import sys
        from contextlib import contextmanager

        @contextmanager
        def capture_bootstrap():
            previous = sys.getprofile()

            def observe(frame, ev, value):
                if (
                    ev == "return"
                    and value is not None
                    and frame.f_code is bootstrap_code
                    and frame.f_globals is bootstrap_globals
                ):
                    fixture.selected = value
                if previous is not None:
                    previous(frame, ev, value)

            try:
                sys.setprofile(observe)
                yield
            finally:
                sys.setprofile(previous)

        class Provider:
            name = "ollama"
            model = "deepseek-v4.1-flash:cloud"

            def complete(self, request):
                fixture.provider_calls += 1
                from review_sensei.models import ProviderResponse

                return ProviderResponse(
                    text=json.dumps(fixture.reply_document),
                    provider=self.name,
                    model=self.model,
                )

        class Registry:
            def create(self, settings):
                return Provider()

        output, errors = io.StringIO(), io.StringIO()
        old = Path.cwd()
        try:
            os.chdir(self.root)
            with ExitStack() as stack:
                for context in [
                    patch.dict(os.environ, environment, clear=True),
                    patch.object(review_run, "physical_opener", self.external),
                    patch.object(
                        review_run, "ApiRunRequestTransport", side_effect=caller_factory
                    ),
                    patch.object(
                        review_run, "BrokerClient", side_effect=broker_factory
                    ),
                    patch.object(review_run, "GitHubHttp", side_effect=http_factory),
                    capture_bootstrap(),
                    patch.object(
                        action_reply, "default_registry", return_value=Registry()
                    ),
                    forbid_replacement_phase_budget(),
                    redirect_stdout(output),
                    redirect_stderr(errors),
                ]:
                    stack.enter_context(context)
                result = package_cli.main(
                    [
                        "reply-run",
                        "--policy-commit",
                        self.policy,
                        "--run-directory",
                        str(self.root),
                        *self.extra_cli_flags,
                        *extra,
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
                        str(self.source["id"]),
                        "--source-updated-at",
                        self.source["updated_at"],
                        "--source-kind",
                        self.source_kind_cli,
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
            self.error, self.output = errors.getvalue(), output.getvalue()
            return result
        finally:
            os.chdir(old)


def _invoke_command_with_runner_flags(
    fixture: CommandEntryFixture,
    *,
    runner_flags: tuple[str, ...] = (),
    body: str | None = None,
    kwarg_cancel: bool | None = None,
) -> int:
    if body is not None:
        fixture.select_command(body, new_run=True)
    event = {
        "action": "created",
        "issue": {"number": 7, "pull_request": {}},
        "comment": copy.deepcopy(fixture.source),
    }
    event_file = fixture.root / "command-event.json"
    event_file.write_text(json.dumps(event), encoding="utf-8")

    def caller_factory(**kwargs):
        caller = ApiRunRequestTransport(**kwargs)
        fixture.accounted = caller
        fixture.transports.append(caller)
        return caller

    def broker_factory(**kwargs):
        return BrokerClient(
            broker_url="https://broker.github.test/api/github/token", **kwargs
        )

    def http_factory(**kwargs):
        http = GitHubHttp(api_url="https://api.github.test", **kwargs)
        fixture.http = http
        return http

    environment = {
        **fixture_process_environment(),
        "GITHUB_RUN_ID": str(fixture.run_id),
        "GITHUB_RUN_ATTEMPT": str(fixture.attempt),
        "GITHUB_EVENT_NAME": "issue_comment",
        "GITHUB_EVENT_PATH": str(event_file),
        "REVIEW_SENSEI_WORKFLOW_REF": "malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@refs/tags/v5",
        "REVIEW_SENSEI_WORKFLOW_SHA": "a" * 40,
        "GITHUB_ACTOR": "alice",
        "GITHUB_TRIGGERING_ACTOR": "alice",
        "GITHUB_TOKEN": "synthetic-workflow-read",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "synthetic",
        "ACTIONS_ID_TOKEN_REQUEST_URL": "https://oidc.github.test/token?fixture=1",
        "REVIEWSENSEI_APP_ID": "0",
    }
    output, errors = io.StringIO(), io.StringIO()
    previous_directory = Path.cwd()
    fixture.exception = None
    argv = [
        "--policy-commit",
        fixture.policy,
        "--run-directory",
        str(fixture.root),
        *runner_flags,
        "--",
        "--repository",
        "owner/repo",
        "--repository-id",
        "123",
        "--pull-request",
        "7",
        "--head-sha",
        fixture.requested_head,
        "--source-comment-id",
        str(fixture.SOURCE_ID),
        "--comment-body",
        fixture.command_body,
        "--actor",
        "untrusted-hint",
        "--association",
        "NONE",
        "--config",
        str(fixture.root / ".reviewsensei.yml"),
        "--github-session-ledger",
        "--allow-write",
    ]
    try:
        os.chdir(fixture.root)
        with ExitStack() as stack:
            for scope in (
                patch.dict(os.environ, environment, clear=True),
                patch.object(review_run, "physical_opener", fixture.external),
                patch.object(
                    review_run, "ApiRunRequestTransport", side_effect=caller_factory
                ),
                patch.object(review_run, "BrokerClient", side_effect=broker_factory),
                patch.object(review_run, "GitHubHttp", side_effect=http_factory),
                forbid_replacement_phase_budget(),
                redirect_stdout(output),
                redirect_stderr(errors),
            ):
                stack.enter_context(scope)
            if kwarg_cancel is not None:
                return action_run.run_action(
                    argv,
                    purpose="command",
                    cancel_unstarted_on_source_mutation=kwarg_cancel,
                )
            return package_cli.main(["command-run", *argv])
    except Exception as error:
        fixture.exception = error
        errors.write(str(error))
        return 2 if kwarg_cancel is None else 1
    finally:
        os.chdir(previous_directory)
        fixture.stdout, fixture.error = output.getvalue(), errors.getvalue()


class _NetworkGuardMixin(unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        for target in ("socket.create_connection", "socket.socket.connect"):
            guard = patch(target, side_effect=AssertionError("unexpected network"))
            guard.start()
            self.addCleanup(guard.stop)


class PreRolloverCommandSyntaxValidationTests(_NetworkGuardMixin):
    def test_unrecognized_command_fails_before_bootstrap_and_leaves_root_intact(self):
        unrecognized = (
            "hello world",
            "@reviewsensei",
            "@reviewsensei unsupported-command",
            "@reviewsensei rescan",
            "@reviewsensei please re-scan",
        )
        # 1. Cold session (no root comment exists yet): zero HTTP calls, zero comments.
        for body in unrecognized:
            with (
                self.subTest(phase="cold", body=body),
                tempfile.TemporaryDirectory() as d,
            ):
                f = CommandEntryFixture(d)
                with patch.object(
                    action_run,
                    "bootstrap",
                    side_effect=AssertionError("bootstrap must not run"),
                ):
                    rc = _invoke_command_with_runner_flags(f, body=body)
                self.assertEqual(rc, 1, f.error)
                self.assertIn(
                    "authenticated source is not a supported command", f.error
                )
                self.assertEqual(f.trace, [])
                self.assertEqual(f.broker_requests, [])
                self.assertIsNone(f.comment)

        # 2. Existing terminal review root: predecessor root stays untouched (no rollover).
        with tempfile.TemporaryDirectory() as directory:
            f = CommandEntryFixture(directory)
            self.assertEqual(f.invoke(human=True), 0, f.error)
            saved_comment = copy.deepcopy(f.comment)
            saved_reviews = copy.deepcopy(f.reviews)
            predecessor = parse_session_comment(f.comment["body"], identity=IDENTITY)
            assert predecessor is not None
            f.trace.clear()
            f.broker_requests.clear()

            malformed = (
                (
                    "@reviewsensei override RS-123456 " + "RS-ABCDEF " * 10,
                    "authenticated source is not a supported command",
                ),
                (
                    '@reviewsensei accept-coverage-uncertainty --reason "   "',
                    "maintainer disposition requires a reason",
                ),
                (
                    f"@reviewsensei dismiss {'a' * 64} --reason {'x' * 600}",
                    "exceeds the bound",
                ),
            )
            for idx, body in enumerate(unrecognized):
                with self.subTest(phase="warm-unrecognized", body=body):
                    f.SOURCE_ID = 400 + idx
                    with patch.object(
                        action_run,
                        "bootstrap",
                        side_effect=AssertionError("bootstrap must not run"),
                    ):
                        rc = _invoke_command_with_runner_flags(f, body=body)
                    self.assertEqual(rc, 1, f.error)
                    self.assertIn(
                        "authenticated source is not a supported command", f.error
                    )
                    self.assertEqual(f.trace, [])
                    self.assertEqual(f.broker_requests, [])
                    self.assertEqual(f.comment, saved_comment)
                    self.assertEqual(f.reviews, saved_reviews)
                    self.assertEqual((f.business_writes, f.closure_writes), (0, 0))
                    current = parse_session_comment(
                        f.comment["body"], identity=IDENTITY
                    )
                    assert current is not None
                    self.assertEqual(current.record_sha256, predecessor.record_sha256)
                    self.assertIsNone(current.api_run_accounting["action"])

            for idx, (body, msg) in enumerate(malformed):
                with self.subTest(phase="warm-malformed", msg=msg):
                    f.SOURCE_ID = 450 + idx
                    with patch.object(
                        action_run,
                        "bootstrap",
                        side_effect=AssertionError("bootstrap must not run"),
                    ):
                        rc = _invoke_command_with_runner_flags(f, body=body)
                    self.assertEqual(rc, 1, f.error)
                    self.assertIn(msg, f.error)
                    self.assertEqual(f.trace, [])
                    self.assertEqual(f.comment, saved_comment)

            # A subsequent valid command rolls over cleanly.
            f.SOURCE_ID = 499
            f.select_command("@reviewsensei review status", new_run=True)
            self.assertEqual(f.invoke_command(), 0, f.error)


class OptInTrustEnrollmentRefusalTests(_NetworkGuardMixin):
    def test_cancel_unstarted_flag_with_trust_enrollment_refuses_before_bootstrap(self):
        with tempfile.TemporaryDirectory() as directory:
            f = CommandEntryFixture(directory)
            with patch.object(
                action_run,
                "bootstrap",
                side_effect=AssertionError("bootstrap must not run"),
            ):
                rc = _invoke_command_with_runner_flags(
                    f,
                    runner_flags=(
                        "--cancel-unstarted-on-source-mutation",
                        "--enable-current-trust-enrollment",
                    ),
                    body="@reviewsensei review pause",
                )
            self.assertEqual(rc, 1, f.error)
            self.assertIn(
                "unsupported with current trust enrollment",
                f.error,
            )
            self.assertEqual(f.trace, [])
            self.assertIsNone(f.comment)

    def test_defense_in_depth_refuses_when_trust_verification_profile_present(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ReplyFixture(directory)
            run, args = fixture.prepare()
            with patch(
                "review_sensei_github.trust_effects.verification_profile",
                return_value=object(),
            ):
                with self.assertRaisesRegex(
                    ReviewInputError,
                    "unsupported with trust verification profile",
                ):
                    execute_reply(
                        run,
                        args,
                        cancel_unstarted_on_source_mutation=True,
                    )
            self.assertIsNone(fixture.action())

        with tempfile.TemporaryDirectory() as directory:
            f = CommandEntryFixture(directory)
            captured: list[tuple[object, object]] = []
            unwrapped_execute = inspect.unwrap(execute_command)

            def capture(run, args):
                captured.append((run, args))
                return 0

            with patch.object(action_run, "execute_command", capture):
                self.assertEqual(
                    f.invoke_command("@reviewsensei review pause"), 0, f.error
                )
            run, args = captured[0]
            args.cancel_unstarted_on_source_mutation = True  # type: ignore[attr-defined]
            with patch(
                "review_sensei_github.trust_effects.verification_profile",
                return_value=object(),
            ):
                with self.assertRaisesRegex(
                    ReviewInputError,
                    "unsupported with trust verification profile",
                ):
                    unwrapped_execute(run, args)  # type: ignore[arg-type]
            rec = parse_session_comment(f.comment["body"], identity=IDENTITY)
            assert rec is not None
            self.assertIsNone(rec.api_run_accounting["action"])


class CommandRunG32CliTests(_NetworkGuardMixin):
    def test_default_without_flag_still_holds_pending_on_edit_and_delete(self):
        for mutation in ("edited", "deleted"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as d:
                f = _PreEffectCommandFixture(d)
                f.SOURCE_ID = 101
                f.select_command("@reviewsensei review pause")

                def mutate(current: _PreEffectCommandFixture, kind=mutation) -> None:
                    if current.source_reads == 1:
                        if kind == "edited":
                            current.source["body"] = "@reviewsensei review pause edited"
                            current.source["updated_at"] = "2026-10-26T00:05:00Z"
                        else:
                            current.deleted_source_ids.add(current.SOURCE_ID)

                f.before_source_read = mutate
                rc = _invoke_command_with_runner_flags(f)
                self.assertEqual(rc, 1, f.error)
                rec = parse_session_comment(f.comment["body"], identity=IDENTITY)
                assert rec is not None
                self.assertFalse(rec.operator_paused)
                self.assertEqual(f.business_writes, 0)
                self.assertEqual(rec.api_run_accounting["action"]["phase"], "pending")
                self.assertFalse(rec.api_run_accounting["action"]["execution_started"])

    def test_opt_in_cli_cancels_unstarted_command_on_edit_restored_and_delete_then_recovers(
        self,
    ):
        for mutation, use_kwarg in (
            ("edited", False),
            ("edit_restored", False),
            ("deleted", False),
            ("deleted", True),
        ):
            with (
                self.subTest(mutation=mutation, use_kwarg=use_kwarg),
                tempfile.TemporaryDirectory() as d,
            ):
                f = _PreEffectCommandFixture(d)
                # Establish clean initial review history/charges first.
                self.assertEqual(f.invoke(), 0, f.error)
                initial_reviews = len(f.reviews)

                f.SOURCE_ID = 201
                f.select_command("@reviewsensei review pause", new_run=True)

                def mutate_before_effect(
                    current: _PreEffectCommandFixture, kind=mutation
                ) -> None:
                    if current.source_reads == 1:
                        if kind == "edited":
                            current.source["body"] = "@reviewsensei review pause edited"
                            current.source["updated_at"] = "2026-10-26T00:05:00Z"
                        elif kind == "edit_restored":
                            current.source["updated_at"] = "2026-10-26T00:06:00Z"
                        else:
                            current.deleted_source_ids.add(current.SOURCE_ID)

                f.before_source_read = mutate_before_effect
                rc = _invoke_command_with_runner_flags(
                    f,
                    runner_flags=()
                    if use_kwarg
                    else ("--cancel-unstarted-on-source-mutation",),
                    kwarg_cancel=True if use_kwarg else None,
                )
                f.before_source_read = None
                self.assertEqual(rc, 0, f.error + f.stdout)
                self.assertIn("Unstarted action canceled after source change", f.stdout)
                self.assertEqual(f.business_writes, 0)

                rec = parse_session_comment(f.comment["body"], identity=IDENTITY)
                assert rec is not None
                self.assertFalse(rec.operator_paused)
                state = ApiRunAccounting.from_document(rec.api_run_accounting)
                assert state.action is not None
                expected_reason = (
                    "source_deleted" if mutation == "deleted" else "source_edited"
                )
                self.assertEqual(state.action["phase"], "terminal")
                self.assertFalse(state.action["execution_started"])
                self.assertEqual(
                    state.action["outcome"],
                    {"kind": "canceled_unstarted", "reason": expected_reason},
                )
                self.assertGreater(state.confirmed, 0)
                self.assertTrue(state.blocks)

                retained = read_action_result(f.selected.ledger, IDENTITY, rec)
                self.assertEqual(
                    retained["outcome"],
                    {"kind": "canceled_unstarted", "reason": expected_reason},
                )
                self.assertEqual(
                    retained["result"]["authority_mode"], "same_owner_active"
                )
                self.assertEqual(retained["result"]["target_run_id"], f.run_id)

                # Subsequent command rolls over cleanly.
                f.SOURCE_ID = 202
                f.select_command("@reviewsensei review status", new_run=True)
                self.assertEqual(f.invoke_command(), 0, f.error)

                # Subsequent full PR review on a new head recovers and approves cleanly.
                f.command_active = False
                f.past_jobs[f.run_id] = f.owner_job
                f.run_id += 1
                f.owner_job = "hosted"
                f.event_name = "pull_request"
                f.requested_head = f.live_head = "9" * 40
                f.trace.clear()
                f.transports.clear()
                f.worker_observations.clear()
                f.provider_calls = 0
                self.assertEqual(f.invoke(), 0, f.error)
                self.assertGreater(len(f.reviews), initial_reviews)
                self.assertTrue(any(r["state"] == "APPROVED" for r in f.reviews))

    def test_historical_command_opt_in_cancels_on_midflight_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            f = historical_command_case(directory)
            before = parse_session_comment(f.comment["body"], identity=IDENTITY)
            assert before is not None and before.historical_assessments is not None
            hold = copy.deepcopy(before.historical_assessments)
            f.SOURCE_ID = 250
            f.select_command(historical_body(hold), new_run=True)

            deleted = False
            orig_ext = f.external

            def ext_deleted_on_fence(req, timeout):
                nonlocal deleted
                p = urlsplit(req.full_url).path
                if (
                    req.get_method() == "GET"
                    and p == f"/repos/owner/repo/issues/comments/{f.SOURCE_ID}"
                ):
                    f._meter(req)
                    f.source_reads += 1
                    deleted = True
                    return json_response({"message": "Not Found"}, status=404)
                return orig_ext(req, timeout)

            f.external = ext_deleted_on_fence  # type: ignore[method-assign]
            rc = _invoke_command_with_runner_flags(
                f,
                runner_flags=("--cancel-unstarted-on-source-mutation",),
            )
            f.external = orig_ext  # type: ignore[method-assign]
            self.assertTrue(deleted)
            self.assertEqual(rc, 0, f.error + f.stdout)
            self.assertIn("Unstarted action canceled after source change", f.stdout)
            rec = parse_session_comment(f.comment["body"], identity=IDENTITY)
            assert rec is not None
            self.assertEqual(rec.historical_assessments, hold)
            self.assertEqual(rec.api_run_accounting["action"]["phase"], "terminal")
            self.assertEqual(
                rec.api_run_accounting["action"]["outcome"],
                {"kind": "canceled_unstarted", "reason": "source_deleted"},
            )


class ReplyRunG32CliTests(_NetworkGuardMixin):
    def test_default_reply_adds_no_extra_fence_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ReplyFixture(directory)
            run, args = fixture.prepare()
            fixture.invoke(run, args, ReplyResult("replied", comment_id=91))
            fence_reads = sum(
                method == "GET" and path.endswith("/issues/comments/41")
                for _, method, path in fixture.trace
            )
            self.assertEqual(fence_reads, 1)
            self.assertEqual(fixture.action()["phase"], "terminal")

    def test_opt_in_reply_cli_cancels_on_edit_and_delete_for_issue_and_inline_and_recovers(
        self,
    ):
        for kind, mutation in (
            ("issue", "edited"),
            ("issue", "deleted"),
            ("inline", "edited"),
            ("inline", "deleted"),
        ):
            with (
                self.subTest(kind=kind, mutation=mutation),
                tempfile.TemporaryDirectory() as d,
            ):
                f = _PreEffectReplyFixture(d)
                # Initial clean review establishes prior session history.
                f.event_name = "pull_request"
                self.assertEqual(f.invoke(), 0, f.error)
                initial_reviews = len(f.reviews)
                f.past_jobs[f.run_id] = f.owner_job
                f.run_id += 1
                f.owner_job = "hosted"
                f.trace.clear()
                f.transports.clear()
                f.worker_observations.clear()
                f.provider_calls = 0

                if kind == "inline":
                    f.prepare_inline_reply(source_id=41)
                else:
                    f.prepare_reply(source_id=41)

                def mutate_after_admit(current: _PreEffectReplyFixture) -> None:
                    # 1st read is pre-admission fence; 2nd read is pre-effect fence after admit_action.
                    if current.source_reads == 2:
                        if mutation == "edited":
                            current.source["body"] = "@reviewsensei edited mid-flight"
                            current.source["updated_at"] = "2026-10-26T00:05:00Z"
                        else:
                            current.source_deleted = True

                f.before_source_read = mutate_after_admit
                rc = f.invoke_reply_cli(
                    extra=("--cancel-unstarted-on-source-mutation",)
                )
                f.before_source_read = None
                self.assertEqual(rc, 0, f.error + f.output)
                self.assertIn("Unstarted action canceled after source change", f.output)
                self.assertEqual(f.provider_calls, 0)
                self.assertEqual(f.reply_posts, 0)
                self.assertEqual(f.reactions, [])

                rec = parse_session_comment(f.comment["body"], identity=IDENTITY)
                assert rec is not None
                state = ApiRunAccounting.from_document(rec.api_run_accounting)
                assert state.action is not None
                expected_reason = (
                    "source_deleted" if mutation == "deleted" else "source_edited"
                )
                self.assertEqual(state.action["phase"], "terminal")
                self.assertFalse(state.action["execution_started"])
                self.assertEqual(
                    state.action["outcome"],
                    {"kind": "canceled_unstarted", "reason": expected_reason},
                )
                retained = read_action_result(f.selected.ledger, IDENTITY, rec)
                self.assertEqual(
                    retained["result"]["authority_mode"], "same_owner_active"
                )
                self.assertGreater(state.confirmed, 0)

                # Subsequent reply on a fresh comment rolls over and publishes cleanly!
                f.source_deleted = False
                f.past_jobs[f.run_id] = f.owner_job
                f.run_id += 1
                f.trace.clear()
                f.transports.clear()
                f.worker_observations.clear()
                f.provider_calls = 0
                f.prepare_reply(source_id=42)
                rc_next = f.invoke_reply_cli()
                self.assertEqual(rc_next, 0, f.error + f.output)
                self.assertEqual(f.provider_calls, 1)
                self.assertEqual(f.reply_posts, 1)

                # Subsequent PR review also rolls over and approves cleanly!
                f.past_jobs[f.run_id] = f.owner_job
                f.run_id += 1
                f.event_name = "pull_request"
                f.requested_head = f.live_head = "9" * 40
                f.trace.clear()
                f.transports.clear()
                f.worker_observations.clear()
                f.provider_calls = 0
                self.assertEqual(f.invoke(), 0, f.error)
                self.assertGreater(len(f.reviews), initial_reviews)
                self.assertTrue(any(r["state"] == "APPROVED" for r in f.reviews))


class G32CliFailClosedCounterexamplesTests(_NetworkGuardMixin):
    def test_owner_takeover_and_tampered_root_refuse_even_with_flag(self):
        for fault in ("owner_takeover", "tampered_root"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as d:
                f = CommandEntryFixture(d)
                f.SOURCE_ID = 501
                f.select_command("@reviewsensei review pause")

                def tamper(current: CommandEntryFixture, kind=fault) -> None:
                    if current.source_reads != 1:
                        return
                    current.source["body"] += " edited"
                    current.source["updated_at"] = "2026-10-26T00:05:00Z"

                    def mutate_record(record):
                        if kind == "owner_takeover":
                            state = ApiRunAccounting.from_document(
                                record.api_run_accounting
                            )
                            return record.evolve(
                                generation=record.generation + 1,
                                api_run_accounting=state.claim_startup(
                                    ApiExecutionOwner(2, "command", 1)
                                ).to_document(),
                            )
                        return record.evolve(generation=record.generation + 1)

                    current.rewrite_record(mutate=mutate_record)

                f.before_source_read = tamper
                rc = _invoke_command_with_runner_flags(
                    f,
                    runner_flags=("--cancel-unstarted-on-source-mutation",),
                )
                self.assertEqual(rc, 1, f.error)
                self.assertIn("unstarted cancellation root changed", f.error)
                self.assertEqual(f.business_writes, 0)
                rec = parse_session_comment(f.comment["body"], identity=IDENTITY)
                assert rec is not None
                self.assertEqual(rec.api_run_accounting["action"]["phase"], "pending")
                self.assertIsNone(rec.api_run_accounting["action"]["outcome"])

    def test_started_boundary_uncertain_unchanged_head_and_http500_refuse_fail_closed(
        self,
    ):
        # 1. execution_started=True / _action_effect_boundary set -> refuses fail-closed
        with tempfile.TemporaryDirectory() as d:
            fixture = ReplyFixture(d)
            run, args = fixture.prepare()
            args.cancel_unstarted_on_source_mutation = True
            source = run.session_authority["source"]
            intent = run.caller.admit_action(
                run.ledger,
                run.identity,
                purpose="reply",
                source=source,
                head_sha=run.head_sha,
                operation_key="e" * 64,
            )
            run.caller.begin_action_effect(run.ledger, run.identity, intent)
            with self.assertRaises(GitHubBrokerClientError):
                cancel_unstarted_after_source_mutation(
                    run,
                    intent=intent,
                    fence_error=GitHubBrokerClientError(
                        "Current action source changed"
                    ),
                )
            self.assertEqual(fixture.action()["phase"], "pending")
            self.assertTrue(fixture.action()["execution_started"])

        # 2. uncertain > 0 (both via accounting pending request and _action_unresolved) -> refuses
        with tempfile.TemporaryDirectory() as d:
            fixture = ReplyFixture(d)
            run, _args = fixture.prepare()
            source = run.session_authority["source"]
            intent = run.caller.admit_action(
                run.ledger,
                run.identity,
                purpose="reply",
                source=source,
                head_sha=run.head_sha,
                operation_key="f" * 64,
            )
            # Simulate an unknown physical mutation in observations & accounting blocks.
            run.caller._state = replace(
                run.caller.accounting,
                blocks=(
                    replace(
                        run.caller.accounting.blocks[0],
                        pending=(PendingApiRequest("c-0-99", 9_999_999_999_999),),
                    ),
                    *run.caller.accounting.blocks[1:],
                ),
            )
            self.assertGreater(run.caller.accounting.uncertain, 0)
            with self.assertRaises(GitHubBrokerClientError):
                cancel_unstarted_after_source_mutation(
                    run,
                    intent=intent,
                    fence_error=GitHubBrokerClientError(
                        "Current action source changed"
                    ),
                )
            self.assertEqual(run.caller.accounting.action["phase"], "pending")

            # Also verify _action_unresolved() via actual uncertain transport request
            unresolved_dir = Path(d) / "unresolved"
            unresolved_dir.mkdir(parents=True, exist_ok=True)
            fixture2 = ReplyFixture(str(unresolved_dir))
            run2, _args2 = fixture2.prepare()
            intent2 = run2.caller.admit_action(
                run2.ledger,
                run2.identity,
                purpose="reply",
                source=run2.session_authority["source"],
                head_sha=run2.head_sha,
                operation_key="1" * 64,
            )
            with self.assertRaises(TimeoutError):
                with run2.caller.open(
                    Request(
                        "https://api.github.test/synthetic-unknown-effect",
                        data=b"{}",
                        method="POST",
                    ),
                    timeout=10,
                ) as response:
                    response.read(1)
            self.assertTrue(run2.caller._action_unresolved())
            with self.assertRaises(GitHubBrokerClientError):
                cancel_unstarted_after_source_mutation(
                    run2,
                    intent=intent2,
                    fence_error=GitHubBrokerClientError(
                        "Current action source changed"
                    ),
                )
            self.assertEqual(fixture2.action()["phase"], "pending")

        # 3. Unchanged (updated_at, body_sha256) with association change -> refuses fail-closed
        with tempfile.TemporaryDirectory() as d:
            f = CommandEntryFixture(d)
            f.SOURCE_ID = 502
            f.select_command("@reviewsensei review pause")

            def change_association_only(current: CommandEntryFixture) -> None:
                if current.source_reads == 1:
                    current.source["author_association"] = "CONTRIBUTOR"

            f.before_source_read = change_association_only
            rc = _invoke_command_with_runner_flags(
                f,
                runner_flags=("--cancel-unstarted-on-source-mutation",),
            )
            self.assertEqual(rc, 1, f.error)
            self.assertIn("live action source is unchanged", f.error)
            rec = parse_session_comment(f.comment["body"], identity=IDENTITY)
            assert rec is not None
            self.assertEqual(rec.api_run_accounting["action"]["phase"], "pending")

        # 4. PR head changed while comment unchanged -> refuses with head changed
        with tempfile.TemporaryDirectory() as d:
            f = CommandEntryFixture(d)
            f.SOURCE_ID = 503
            f.select_command("@reviewsensei review pause")

            def change_head_only(current: CommandEntryFixture) -> None:
                if current.source_reads == 1:
                    current.live_head = "9" * 40

            f.before_source_read = change_head_only
            rc = _invoke_command_with_runner_flags(
                f,
                runner_flags=("--cancel-unstarted-on-source-mutation",),
            )
            self.assertEqual(rc, 1, f.error)
            self.assertIn("Current action head changed", f.error)
            rec = parse_session_comment(f.comment["body"], identity=IDENTITY)
            assert rec is not None
            self.assertEqual(rec.api_run_accounting["action"]["phase"], "pending")

        # 5. HTTP 500 on live comment read during G32 check -> refuses fail-closed
        with tempfile.TemporaryDirectory() as d:
            f = _PreEffectReplyFixture(d)

            def fail_with_500_on_g32(current: _PreEffectReplyFixture) -> None:
                if current.source_reads == 2:
                    current.source["body"] = "edited"
                    current.source["updated_at"] = "2026-10-26T00:05:00Z"
                elif current.source_reads == 3:
                    current.source_status_override = 500

            f.before_source_read = fail_with_500_on_g32
            rc = f.invoke_reply_cli(extra=("--cancel-unstarted-on-source-mutation",))
            self.assertEqual(rc, 1, f.error)
            self.assertIn("observation failed with HTTP 500", f.error)
            self.assertEqual(f.action()["phase"], "pending")
            self.assertEqual(f.provider_calls, 0)

        # 6. Pre-admit source change on reply-run still refuses BEFORE admit_action (action=None)
        with tempfile.TemporaryDirectory() as d:
            f = _PreEffectReplyFixture(d)

            def edit_before_admit(current: _PreEffectReplyFixture) -> None:
                if current.source_reads == 1:
                    current.source["body"] = "edited before admit"

            f.before_source_read = edit_before_admit
            rc = f.invoke_reply_cli(extra=("--cancel-unstarted-on-source-mutation",))
            self.assertEqual(rc, 1, f.error)
            self.assertIn("Current action source changed", f.error)
            self.assertIsNone(f.action())
            self.assertEqual(f.provider_calls, 0)


if __name__ == "__main__":
    unittest.main()
