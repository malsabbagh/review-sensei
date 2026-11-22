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
        self.source.pop("issue_url", None)
        self.source["pull_request_url"] = (
            "https://api.github.test/repos/owner/repo/pulls/7"
        )
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


class G32GapHardeningTests(_NetworkGuardMixin):
    def test_cold_attempt2_midflight_edit_refuses_cli_cancel_without_owner_stamping(
        self,
    ):
        from review_sensei_github.unstarted_action_recovery import (
            NoBusinessDispatchEvidence,
        )

        # 1. End-to-end Attempt-2 command retry with mid-flight edit + CLI opt-in refuses
        #    without canceling Attempt 1's stranded intent (G32-1).
        with tempfile.TemporaryDirectory() as d:
            f = _PreEffectCommandFixture(d)
            f.SOURCE_ID = 601
            f.select_command("@reviewsensei review pause")
            orig_source = copy.deepcopy(f.source)

            def edit_on_first_fence(current: _PreEffectCommandFixture) -> None:
                if current.source_reads == 1:
                    current.source["body"] = "@reviewsensei review pause edited"
                    current.source["updated_at"] = "2026-10-26T00:05:00Z"

            f.before_source_read = edit_on_first_fence
            rc_a1 = f.invoke_command()
            f.before_source_read = None
            self.assertNotEqual(rc_a1, 0)
            a1_rec = parse_session_comment(f.comment["body"], identity=IDENTITY)
            assert a1_rec is not None
            self.assertEqual(a1_rec.api_run_accounting["owner"]["run_attempt"], 1)

            # Retry on cold Attempt 2 with the opt-in CLI flag while live source edits
            # mid-flight after re-entry -> cancel_unstarted_after_source_mutation MUST refuse!
            f.attempt = 2
            f.source = copy.deepcopy(orig_source)
            f.source_reads = 0

            def edit_midflight_attempt2(current: _PreEffectCommandFixture) -> None:
                if current.source_reads == 2:
                    current.source["body"] = "@reviewsensei review pause edited on a2"
                    current.source["updated_at"] = "2026-10-26T00:06:00Z"

            f.before_source_read = edit_midflight_attempt2
            rc_a2 = _invoke_command_with_runner_flags(
                f,
                runner_flags=("--cancel-unstarted-on-source-mutation",),
            )
            f.before_source_read = None
            self.assertEqual(rc_a2, 1, f.error)
            self.assertIn(
                "same-run unstarted recovery requires original attempt owner", f.error
            )
            self.assertEqual(f.business_writes, 0)
            self.assertEqual(f.provider_calls, 0)
            after_a2 = parse_session_comment(f.comment["body"], identity=IDENTITY)
            assert after_a2 is not None
            self.assertEqual(after_a2.api_run_accounting["action"]["phase"], "pending")
            self.assertIsNone(after_a2.api_run_accounting["action"]["outcome"])

        # 2. Direct owner-mismatch guard on cancel_unstarted_after_source_mutation &
        #    NoBusinessDispatchEvidence.from_stranded_caller refuses Attempt 2 owner stamping.
        with tempfile.TemporaryDirectory() as d2:
            fixture = ReplyFixture(d2)
            run, _args = fixture.prepare()
            source = run.session_authority["source"]
            intent = run.caller.admit_action(
                run.ledger,
                run.identity,
                purpose="reply",
                source=source,
                head_sha=run.head_sha,
                operation_key="1" * 64,
            )
            stranded_rec = parse_session_comment(
                fixture.comment["body"], identity=IDENTITY
            )
            assert stranded_rec is not None
            run.caller._state = run.caller.accounting.claim_startup(
                ApiExecutionOwner(2, "local", 1)
            )
            with self.assertRaisesRegex(
                ReviewInputError, "stranded transport source/root/owner binding"
            ):
                NoBusinessDispatchEvidence.from_stranded_caller(
                    run.caller, identity=IDENTITY, record=stranded_rec
                )
            with self.assertRaisesRegex(
                ReviewInputError,
                "same-run unstarted recovery requires original attempt",
            ):
                cancel_unstarted_after_source_mutation(
                    run,
                    intent=intent,
                    fence_error=GitHubBrokerClientError(
                        "Current action source changed"
                    ),
                )

    def test_cold_restart_with_only_accepted_observation_and_actions_evidence_refuses(
        self,
    ):
        from review_sensei_github.accepted_action import (
            AcceptedActionObservation,
            observe_selected_action,
        )
        from review_sensei_github.api_run_owner import ApiRunActionsEvidence
        from review_sensei_github.api_run_requests import ApiRunSessionLedger
        from review_sensei_github.source_comment_lifecycle import (
            execute_source_lifecycle_route,
        )
        from review_sensei_github.unstarted_action_recovery import (
            authenticate_unstarted_cancellation,
        )
        from test_unstarted_action_recovery_g32 import G32LifecycleFixture

        with tempfile.TemporaryDirectory() as d:
            f = G32LifecycleFixture(d, limit=1024)
            native, configuration, _ev, owner = f.prelude()
            f.accounted.initialize_and_activate(native, IDENTITY, configuration, owner)
            ledger = ApiRunSessionLedger(native, f.accounted, IDENTITY)
            f.accounted.admit_action(
                ledger,
                IDENTITY,
                purpose="command",
                source={
                    "kind": "issue_comment",
                    "id": 41,
                    "updated_at": "2026-10-25T12:00:00Z",
                    "body_sha256": hashlib.sha256(
                        b"@reviewsensei review pause"
                    ).hexdigest(),
                },
                head_sha=f.comment["head"] if "head" in f.comment else "b" * 40,
                operation_key="2" * 64,
            )
            native_2 = f.recover_lifecycle()
            succ = ApiRunSessionLedger(native_2, f.accounted, IDENTITY)
            stranded = parse_session_comment(f.comment["body"], identity=IDENTITY)
            assert stranded is not None
            obs = observe_selected_action(succ, IDENTITY, stranded)
            self.assertIsInstance(obs, AcceptedActionObservation)
            f.source_status = 404
            f.source_payload = {"message": "Not Found"}
            actions_ev = ApiRunActionsEvidence(
                succ.http,
                token="synthetic-actions",
                command_run_id="456",
                job="hosted",
            )
            with self.assertRaisesRegex(
                ReviewInputError, "requires explicit NoBusinessDispatchEvidence"
            ):
                authenticate_unstarted_cancellation(
                    caller=f.accounted,
                    ledger=succ,
                    identity=IDENTITY,
                    record=stranded,
                    actions_evidence=actions_ev,
                    mode="same_run",
                )
            with self.assertRaisesRegex(
                ReviewInputError, "requires explicit NoBusinessDispatchEvidence"
            ):
                execute_source_lifecycle_route(
                    caller=f.accounted,
                    ledger=succ,
                    identity=IDENTITY,
                    record=stranded,
                    actions_evidence=actions_ev,
                    mode="same_run",
                )

    def test_copy_and_deepcopy_of_dispatch_evidence_and_witness_refuse(self):
        from review_sensei_github.api_run_owner import ApiRunActionsEvidence
        from review_sensei_github.api_run_requests import ApiRunSessionLedger
        from review_sensei_github.unstarted_action_recovery import (
            NoBusinessDispatchEvidence,
            authenticate_unstarted_cancellation,
            require_witness_issued,
            verify_no_business_dispatch_evidence,
        )
        from test_unstarted_action_recovery_g32 import G32LifecycleFixture

        with tempfile.TemporaryDirectory() as d:
            f = G32LifecycleFixture(d, limit=1024)
            native, configuration, _ev, owner = f.prelude()
            f.accounted.initialize_and_activate(native, IDENTITY, configuration, owner)
            ledger = ApiRunSessionLedger(native, f.accounted, IDENTITY)
            intent = f.accounted.admit_action(
                ledger,
                IDENTITY,
                purpose="command",
                source={
                    "kind": "issue_comment",
                    "id": 41,
                    "updated_at": "2026-10-25T12:00:00Z",
                    "body_sha256": hashlib.sha256(
                        b"@reviewsensei review pause"
                    ).hexdigest(),
                },
                head_sha="b" * 40,
                operation_key="3" * 64,
            )
            rec = parse_session_comment(f.comment["body"], identity=IDENTITY)
            assert rec is not None
            dispatch_ev = NoBusinessDispatchEvidence.from_stranded_caller(
                f.accounted, identity=IDENTITY, record=rec
            )
            f.source_status = 404
            f.source_payload = {"message": "Not Found"}
            actions_ev = ApiRunActionsEvidence(
                ledger.http,
                token="synthetic-actions",
                command_run_id="456",
                job="hosted",
            )
            state = ApiRunAccounting.from_document(rec.api_run_accounting)
            intent_sha = dispatch_ev.intent_sha256
            for clone in (copy.copy(dispatch_ev), copy.deepcopy(dispatch_ev)):
                with self.assertRaisesRegex(
                    ReviewInputError, "unchanged process-issued transport capture"
                ):
                    verify_no_business_dispatch_evidence(
                        caller=f.accounted,
                        ledger=ledger,
                        identity=IDENTITY,
                        state=state,
                        intent=intent,
                        intent_sha256=intent_sha,
                        evidence=clone,
                    )
            witness = authenticate_unstarted_cancellation(
                caller=f.accounted,
                ledger=ledger,
                identity=IDENTITY,
                record=rec,
                actions_evidence=actions_ev,
                no_business_dispatch_evidence=dispatch_ev,
                mode="same_run",
            )
            for cloned_witness in (copy.copy(witness), copy.deepcopy(witness)):
                with self.assertRaisesRegex(ReviewInputError, "process-issued witness"):
                    require_witness_issued(cloned_witness)
                with self.assertRaisesRegex(ReviewInputError, "process-issued witness"):
                    f.accounted.cancel_unstarted_action(
                        ledger, IDENTITY, cloned_witness
                    )

    def test_two_successor_cas_and_canonical_coordinator_races(self):
        from review_sensei_github.api_run_history import read_previous_run
        from review_sensei_github.api_run_requests import ApiRunSessionLedger
        from review_sensei_github.session_ledger import GitHubIssueCommentSessionLedger
        from review_sensei_github.unstarted_action_recovery import (
            authenticate_unstarted_cancellation,
        )
        from test_unstarted_action_recovery_g32 import (
            G32LifecycleFixture,
            _make_canonical_coordinator,
        )

        from review_sensei.api_work import ApiRunIdentity
        from review_sensei.recovery_authority import AuthorityRefusal

        with tempfile.TemporaryDirectory() as d:
            f = G32LifecycleFixture(d, limit=1024)
            native, configuration, _ev, owner = f.prelude()
            f.accounted.initialize_and_activate(native, IDENTITY, configuration, owner)
            ledger = ApiRunSessionLedger(native, f.accounted, IDENTITY)
            f.accounted.admit_action(
                ledger,
                IDENTITY,
                purpose="command",
                source={
                    "kind": "issue_comment",
                    "id": 41,
                    "updated_at": "2026-10-25T12:00:00Z",
                    "body_sha256": hashlib.sha256(
                        b"@reviewsensei review pause"
                    ).hexdigest(),
                },
                head_sha="b" * 40,
                operation_key="4" * 64,
            )
            f.recover_lifecycle()
            stranded = parse_session_comment(f.comment["body"], identity=IDENTITY)
            assert stranded is not None
            f.source_status = 404
            f.source_payload = {"message": "Not Found"}
            f.past_runs[456] = (2, "hosted", "completed")
            f.past_runs[457] = (1, "hosted", "in_progress")
            f.past_runs[458] = (1, "hosted", "in_progress")

            coord, tok_1 = _make_canonical_coordinator(Path(d) / "coord", epoch=1)
            acc_1 = coord.authenticate(tok_1)  # type: ignore[arg-type]
            tok_2, _snap2 = coord.advance_epoch(
                predecessor_token=tok_1,  # type: ignore[arg-type]
                successor_access=replace(
                    acc_1,
                    store_id=tok_1.base_store_id,  # type: ignore[attr-defined]
                    caller_sha256="9" * 64,
                ),
                writer_route="actions_hosted",
                predecessor_ceased=True,
            )
            # Competing advance using stale tok_1 or predecessor_ceased=False refuses!
            with self.assertRaises(AuthorityRefusal):
                coord.advance_epoch(
                    predecessor_token=tok_1,  # type: ignore[arg-type]
                    successor_access=replace(acc_1, caller_sha256="8" * 64),
                    writer_route="actions_hosted",
                    predecessor_ceased=True,
                )
            with self.assertRaises(AuthorityRefusal):
                coord.advance_epoch(
                    predecessor_token=tok_2,  # type: ignore[arg-type]
                    successor_access=replace(acc_1, caller_sha256="7" * 64),
                    writer_route="actions_hosted",
                    predecessor_ceased=False,
                )

            def make_cross_caller(run_no: int):
                f.run_id = run_no
                f.attempt = 1
                f.trace = []
                rdir = Path(d) / f"run{run_no}"
                rdir.mkdir()
                caller = ApiRunRequestTransport(
                    directory=rdir,
                    run=ApiRunIdentity(123, run_no),
                    plan=f.plan,
                    attempt=1,
                    opener=f._external_open,
                )
                f.accounted = caller
                http_c = GitHubHttp(
                    api_url="https://api.github.test", opener=caller.open
                )
                nat_c = GitHubIssueCommentSessionLedger(
                    http_c, token="synthetic", app_slug="reviewsensei[bot]"
                )
                nat_c.load(IDENTITY)
                return caller, nat_c

            # Both successors read stranded R0; winner 457 settles cleanly, loser 458 fails CAS.
            caller_457, nat_457 = make_cross_caller(457)
            caller_457.rollover_unstarted_and_activate(
                nat_457,
                IDENTITY,
                configuration,
                ApiExecutionOwner(1, "hosted", 0),
                stranded,
                lambda rec: authenticate_unstarted_cancellation(
                    caller=caller_457,
                    ledger=nat_457,
                    identity=IDENTITY,
                    record=rec,
                    canonical_coordinator=coord,
                    canonical_token=tok_2,  # type: ignore[arg-type]
                    mode="cross_run",
                ),
            )
            winner_rec = parse_session_comment(f.comment["body"], identity=IDENTITY)
            assert winner_rec is not None
            prev_rec = read_previous_run(
                nat_457, IDENTITY, caller_457.accounting.previous_record
            )
            self.assertEqual(
                prev_rec.api_run_accounting["action"]["outcome"],  # type: ignore[index]
                {"kind": "canceled_unstarted", "reason": "source_deleted"},
            )
            caller_458, nat_458 = make_cross_caller(458)
            with self.assertRaises(ReviewInputError):
                caller_458.rollover_unstarted_and_activate(
                    nat_458,
                    IDENTITY,
                    configuration,
                    ApiExecutionOwner(1, "hosted", 0),
                    stranded,
                    lambda rec: authenticate_unstarted_cancellation(
                        caller=caller_458,
                        ledger=nat_458,
                        identity=IDENTITY,
                        record=rec,
                        canonical_coordinator=coord,
                        canonical_token=tok_2,  # type: ignore[arg-type]
                        mode="cross_run",
                    ),
                )
            # Winner's archived predecessor and active root remain untouched.
            after_race_rec = parse_session_comment(f.comment["body"], identity=IDENTITY)
            assert after_race_rec is not None
            self.assertEqual(after_race_rec.record_sha256, winner_rec.record_sha256)

    def test_g27_3_url_repo_id_and_issue_pr_binding_refusals(self):
        from review_sensei_github.run_authority import VerifiedRunAction
        from review_sensei_github.source_comment_lifecycle import (
            admit_event_comment_source,
            observe_live_source_revision,
        )
        from review_sensei_github.unstarted_action_recovery import (
            _observe_mutated_source,
        )

        from review_sensei.session import SessionIdentity

        body = "@reviewsensei explain this finding"
        updated = "2026-10-25T12:00:00Z"
        source_doc = {
            "kind": "issue_comment",
            "id": 41,
            "updated_at": updated,
            "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
        }

        # 1. admit_event_comment_source repo ID, issue.pull_request, and URL checks
        with self.assertRaisesRegex(
            ReviewInputError, "action source repository does not match event"
        ):
            admit_event_comment_source(
                event_name="issue_comment",
                event_payload={
                    "action": "created",
                    "repository": {"id": 999, "full_name": "owner/repo"},
                    "issue": {"number": 7, "pull_request": {}},
                    "comment": {
                        "id": 41,
                        "body": body,
                        "updated_at": updated,
                        "issue_url": "https://api.github.test/repos/owner/repo/issues/7",
                    },
                },
                purpose="command",
                repository="owner/repo",
                repository_id=123,
                pull_request=7,
                source_comment_id=41,
            )
        with self.assertRaisesRegex(
            ReviewInputError, "issue_comment event requires pull_request association"
        ):
            admit_event_comment_source(
                event_name="issue_comment",
                event_payload={
                    "action": "created",
                    "issue": {"number": 7},  # missing pull_request on issue_comment!
                    "comment": {
                        "id": 41,
                        "body": body,
                        "updated_at": updated,
                        "issue_url": "https://api.github.test/repos/owner/repo/issues/7",
                    },
                },
                purpose="command",
                repository="owner/repo",
                repository_id=123,
                pull_request=7,
                source_comment_id=41,
            )
        with self.assertRaisesRegex(
            ReviewInputError, "action source does not match the original event"
        ):
            admit_event_comment_source(
                event_name="issue_comment",
                event_payload={
                    "action": "created",
                    "issue": {"number": 7, "pull_request": {}},
                    "comment": {
                        "id": 41,
                        "body": body,
                        "updated_at": updated,
                        "issue_url": "https://api.github.test/repos/owner/repo/issues/7?bad=1",
                    },
                },
                purpose="command",
                repository="owner/repo",
                repository_id=123,
                pull_request=7,
                source_comment_id=41,
            )

        # 2. VerifiedRunAction.fence() refuses repo ID mismatch and invalid/swapped comment URLs
        for fault in ("repo_id", "swapped_url", "query_url"):
            with (
                self.subTest(fault=fault),
                tempfile.TemporaryDirectory() as d,
            ):
                fixture = ReplyFixture(d)
                run, _args = fixture.prepare()
                fixture.source.pop("issue_url", None)
                if fault == "swapped_url":
                    fixture.source["pull_request_url"] = (
                        "https://api.github.test/repos/owner/repo/pulls/7"
                    )
                elif fault == "query_url":
                    fixture.source["issue_url"] = (
                        "https://api.github.test/repos/owner/repo/issues/7?extra=1"
                    )
                else:
                    fixture.source["issue_url"] = (
                        "https://api.github.test/repos/owner/repo/issues/7"
                    )
                orig_ext = fixture.accounted._opener
                pr_repo_id = 999 if fault == "repo_id" else 123

                def patched_ext(req, timeout, *, orig=orig_ext, rid=pr_repo_id):
                    resp = orig(req, timeout)
                    p = urlsplit(req.full_url).path
                    if req.get_method() == "GET" and p == "/repos/owner/repo/pulls/7":
                        data = json.loads(resp.read(524288))
                        data["base"]["repo"] = {
                            "id": rid,
                            "full_name": "owner/repo",
                        }
                        return json_response(data)
                    return resp

                with patch.object(
                    fixture.accounted, "_opener", side_effect=patched_ext
                ):
                    verified = VerifiedRunAction(run)
                    with self.assertRaises(GitHubBrokerClientError):
                        verified.fence()

        # 3. observe_live_source_revision & _observe_mutated_source reject non-positive repo_id & bad URL
        with tempfile.TemporaryDirectory() as d:
            fixture = ReplyFixture(d)
            run, _args = fixture.prepare()
            bad_id = SessionIdentity("owner/repo", 7, repository_id=None)
            with self.assertRaisesRegex(ReviewInputError, "positive repository_id"):
                observe_live_source_revision(
                    caller=run.caller,
                    ledger=run.ledger,
                    identity=bad_id,
                    original_source=source_doc,
                    phase="unstarted_intent",
                )
            with self.assertRaisesRegex(ReviewInputError, "positive repository_id"):
                _observe_mutated_source(
                    caller=run.caller,
                    ledger=run.ledger,
                    identity=bad_id,
                    source=source_doc,
                )
            fixture.source["issue_url"] = (
                "https://api.github.test/repos/other/repo/issues/7"
            )
            with self.assertRaisesRegex(ReviewInputError, "malformed"):
                _observe_mutated_source(
                    caller=run.caller,
                    ledger=run.ledger,
                    identity=IDENTITY,
                    source=source_doc,
                )


if __name__ == "__main__":
    unittest.main()
