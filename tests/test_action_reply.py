"""Synthetic reply services over real source-owned caller action checkpoints."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit
from urllib.request import Request

from review_sensei_github.action_completion import _reply_unnecessary, reply_source
from review_sensei_github.action_reply import _outcome, _providers, execute_reply
from review_sensei_github.api_run_history import read_action_result
from review_sensei_github.api_run_host import review_startup_plan
from review_sensei_github.broker_client import BrokerClient
from review_sensei_github.conversation import ReplyResult
from review_sensei_github.operation_comments import ResultPartStore
from review_sensei_github.operation_host import GitHubCommentOperationStore
from review_sensei_github.review_run import ActionRunLedger, ReviewRun
from review_sensei_github.run_authority import AUDIENCE
from review_sensei_github.session_ledger import parse_session_comment

from review_sensei.api_work import ApiRunAccounting
from review_sensei.configuration import load_configuration
from review_sensei.errors import ProviderError, ReviewInputError, ReviewSenseiError
from review_sensei.hosted import plan_hosted_execution
from review_sensei.outcomes import ResourceBudget
from review_sensei.service import ReviewService
from tests import test_api_work_persistence as foundation
from tests.fake_github_http import json_response
from tests.test_api_work_lifecycle import LifecycleFixture
from tests.test_api_work_persistence import BASE, HEAD, IDENTITY

BODY = "@reviewsensei explain the changed branch"
SOURCE = {
    "kind": "issue_comment", "id": 41, "updated_at": "2026-10-26T00:00:00Z",
    "body_sha256": hashlib.sha256(BODY.encode()).hexdigest(),
}
GRANT = "sg1.1700000000000." + "a" * 43


def synthetic_authority(purpose="reply"):
    """Test data only; the real production bootstrap authenticates Worker grants."""
    return {
        "version": 3, "purpose": purpose, "repository": IDENTITY.repository,
        "repository_id": IDENTITY.repository_id, "pull_request": IDENTITY.pull_request,
        "head_sha": HEAD, "run_id": "456", "run_attempt": 1, "job": "local",
        "job_workflow_ref": "malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@refs/tags/v5",
        "job_workflow_sha": "a" * 40, "issued_at": 1700000000,
        "expires_at": 1700000600, "audience": AUDIENCE,
        "source": copy.deepcopy(SOURCE) if purpose == "reply" else {
            "kind": "pull_request", "id": 7, "updated_at": None, "body_sha256": None,
        },
    }


class SyntheticGrantBroker:
    def __init__(self, document):
        self.document = document

    def verify_session_grant(self, grant, document):
        assert grant == GRANT and document == self.document
        return copy.deepcopy(document)


class ReplyFixture(LifecycleFixture):
    def __init__(self, directory):
        super().__init__(directory, limit=2048)
        self.owner_job = "local"
        self.plan = review_startup_plan("local")
        self.config_bytes = (
            b"schema: 1\napi_request_limit: 2048\ninference:\n"
            b"  backend: local-ollama\n  model: qwen3.5:4b\ngithub:\n  writes: true\n"
        )
        self.source = {
            "id": SOURCE["id"], "updated_at": SOURCE["updated_at"], "body": BODY,
            "user": {"login": "alice", "type": "User"}, "author_association": "OWNER",
        }
        self.effects = 0
        self.calls = []
        self.lose_terminal_once = False
        self.terminal_patches = 0

    def _external_open(self, request, timeout):
        path = urlsplit(request.full_url).path
        payload = json.loads(request.data) if request.data else None
        if request.method == "PATCH" and path.endswith("/issues/comments/1000000") and self.accounted._terminal_reason is not None:
            self.terminal_patches += 1
            if self.lose_terminal_once:
                self.lose_terminal_once = False
                super()._external_open(request, timeout)
                raise TimeoutError("synthetic stored terminal write response lost")
        if path.endswith("/issues/comments/41"):
            self.trace.append((self.attempt, request.method, path))
            return json_response(self.source)
        if path.endswith("/pulls/7"):
            self.trace.append((self.attempt, request.method, path))
            return json_response({
                "number": 7, "state": "open", "draft": False, "user": {"login": "alice", "type": "User"},
                "head": {"sha": HEAD, "ref": "feature", "repo": {"full_name": "owner/repo", "fork": False}},
                "base": {"sha": BASE, "ref": "main", "repo": {"id": 123, "full_name": "owner/repo", "fork": False}},
            })
        if path.endswith("/tags"):
            self.trace.append((self.attempt, request.method, path))
            assert request.get_header("Authorization") is None
            return json_response({"models": [{"name": "qwen3.5:4b"}]})
        if "/attempts/" in path and path.endswith("/jobs"):
            self.trace.append((self.attempt, request.method, path))
            return json_response({"total_count": 1, "jobs": [{
                "id": 901, "run_id": 456, "run_attempt": 1,
                "name": "ReviewSensei / local", "status": "in_progress",
                "conclusion": None, "completed_at": None,
            }]})
        if urlsplit(request.full_url).hostname == "broker.github.test":
            self.trace.append((self.attempt, request.method, path))
            response = {
                "token": "synthetic-" + payload["capability"],
                "capability": payload["capability"],
                "control_receipt": {**payload["control_budget"], "dispatched": 1},
                "session_state": "known", "reservation_owner": {"run_id": "456", "head_sha": HEAD},
                "session_grant": GRANT,
                "app_id": 999,
                "session_attestation": {**payload["session_attestation"], "actor": None},
            }
            return json_response(response)
        if path.endswith("/synthetic-unknown-effect"):
            self.trace.append((self.attempt, request.method, path))
            raise TimeoutError("synthetic unknown business write")
        return super()._external_open(request, timeout)

    def prepare(self):
        # Adapt only the foundation fixture's synthetic prelude to closed v3.
        real_client = BrokerClient
        real_transport = foundation.ApiRunRequestTransport
        real_evidence = foundation.ApiRunActionsEvidence
        def source_broker(**kwargs):
            kwargs.setdefault("authority", synthetic_authority("review"))
            return real_client(**kwargs)

        with patch.object(foundation, "BrokerClient", side_effect=source_broker), patch.object(
            foundation, "ApiRunRequestTransport", side_effect=lambda **kw: real_transport(job="local", **kw)
        ), patch.object(
            foundation, "ApiRunActionsEvidence", side_effect=lambda *a, **kw: real_evidence(*a, job="local", **kw)
        ):
            native, configuration, _evidence, owner = self.prelude()
        native.authenticated_app_id = 999
        self.accounted.initialize_and_activate(native, IDENTITY, configuration, owner)
        ledger = ActionRunLedger(native, self.accounted, IDENTITY)
        path = Path(self.directory) / ".reviewsensei.yml"
        path.write_bytes(self.config_bytes)
        plan = plan_hosted_execution(load_configuration(path))
        document = {
            **synthetic_authority(),
            "actor": {"login": "alice", "type": "User", "association": "OWNER"},
        }
        run = ReviewRun(
            self.accounted, ledger, self.http, SyntheticGrantBroker(document),
            "synthetic-oidc", IDENTITY, plan, BASE, HEAD, "main", "owner/repo", path,
            document, GRANT,
        )
        args = Namespace(
            source_comment_id=41, source_updated_at=SOURCE["updated_at"],
            source_kind="issue", app_slug="reviewsensei[bot]", root_comment_id=0,
            profile=None, fixture_response=None, enable_broader_review=False,
            work_recovery_dir=None, work_recovery_key_file=None,
            provider=plan.backend, model=plan.model, base_url=plan.base_url,
        )
        return run, args

    def invoke(self, run, args, result):
        def generate(**kwargs):
            self.calls.append(kwargs)
            if isinstance(result, BaseException):
                raise result
            self.effects += 1
            value = result(**kwargs) if callable(result) else result
            return self.synthetic_generic_completion(value)

        with patch("review_sensei_github.action_reply.application", return_value=SimpleNamespace(
            generate_and_publish_reply=generate,
            reviewer=SimpleNamespace(finalizer=SimpleNamespace()),
        )), patch.dict(os.environ, {"GITHUB_RUN_ID": "456", "GITHUB_RUN_ATTEMPT": "1"}, clear=True), redirect_stdout(io.StringIO()):
            return execute_reply(run, args)

    @staticmethod
    def synthetic_generic_completion(result):
        """A fake generic publisher, never a human/finalization authority."""
        if result.status in {"replied", "already_replied"} and result.finalization_completion is None and all(
            value is None for value in (result.approval_status, result.assessment_status, result.resolution_status,
                                       result.approval_diagnostic, result.assessment_diagnostic)
        ):
            source = reply_source(
                repository=IDENTITY.repository, pull_request=IDENTITY.pull_request,
                head_sha=HEAD, kind="issue", source_id=41, updated_at=SOURCE["updated_at"], body=BODY,
            )
            return replace(result, finalization_completion=_reply_unnecessary(result, source))
        return result

    def action(self):
        return ApiRunAccounting.from_document(parse_session_comment(
            self.comment["body"], identity=IDENTITY
        ).api_run_accounting).action


class ActionReplyTests(unittest.TestCase):
    def test_lost_terminal_response_exact_readback_never_replays_reply_or_patch(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ReplyFixture(directory)
            run, args = fixture.prepare()
            fixture.lose_terminal_once = True
            fixture.invoke(run, args, ReplyResult("replied", comment_id=91))
            self.assertEqual(fixture.action()["phase"], "terminal")
            self.assertEqual(fixture.terminal_patches, 1)
            self.assertEqual(fixture.effects, 1)
            with self.assertRaises(ReviewInputError):
                fixture.invoke(run, args, ReplyResult("replied", comment_id=92))
            self.assertEqual(fixture.terminal_patches, 1)
            self.assertEqual(fixture.effects, 1)

    def test_real_application_noop_uses_current_action_and_shared_budget(self):
        from review_sensei_github.conversation import ConversationPublisher

        with tempfile.TemporaryDirectory() as directory:
            fixture = ReplyFixture(directory)
            run, args = fixture.prepare()
            with patch.object(ConversationPublisher, "prepare_context", return_value=ReplyResult("skipped_no_mention")), patch.dict(
                os.environ, {"GITHUB_RUN_ID": "456", "GITHUB_RUN_ATTEMPT": "1"}, clear=True
            ), redirect_stdout(io.StringIO()):
                self.assertEqual(execute_reply(run, args), 0)
            self.assertEqual(fixture.action()["outcome"], {
                "kind": "explicit_noop", "reason": "not_mentioned",
            })

    def test_success_and_existing_reply_close_exact_durable_action_without_duplicate(self):
        for status, kind in (("replied", "applied"), ("already_replied", "already_applied")):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                fixture = ReplyFixture(directory)
                run, args = fixture.prepare()
                self.assertEqual(fixture.invoke(run, args, ReplyResult(status, comment_id=91)), 0)
                action = fixture.action()
                self.assertEqual(action["phase"], "terminal")
                self.assertEqual(action["outcome"], {"kind": kind, "reason": None})
                record = parse_session_comment(fixture.comment["body"], identity=IDENTITY)
                retained = read_action_result(run.ledger, IDENTITY, record)
                self.assertEqual(retained["result"]["status"], status)
                self.assertIsNone(record.transaction)
                before = fixture.effects
                with self.assertRaises(ReviewInputError):
                    fixture.invoke(run, args, ReplyResult("replied", comment_id=92))
                self.assertEqual(fixture.effects, before)

    def test_explicit_noop_uses_schema_reason_and_preserves_action_identity(self):
        for status, reason in (("skipped_no_mention", "not_mentioned"), ("skipped_unauthorized", "unauthorized")):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                fixture = ReplyFixture(directory)
                run, args = fixture.prepare()
                fixture.invoke(run, args, ReplyResult(status))
                action = fixture.action()
                self.assertEqual(action["outcome"], {"kind": "explicit_noop", "reason": reason})
                self.assertEqual(action["intent"]["source"], SOURCE)

    def test_provider_unknown_and_incomplete_assessment_remain_pending_without_replay(self):
        for result in (
            ProviderError("synthetic provider outcome unknown"),
            ReplyResult("pending_delivery_incomplete"),
            ReplyResult("replied", comment_id=91, assessment_status="insufficient_evidence"),
            ReplyResult("replied", comment_id=91, approval_status="approval_withheld"),
            ReplyResult("replied", comment_id=91, resolution_status="unconfirmed"),
            ReplyResult("skipped_stale_head"),
        ):
            with self.subTest(result=result), tempfile.TemporaryDirectory() as directory:
                fixture = ReplyFixture(directory)
                run, args = fixture.prepare()
                with self.assertRaises((ProviderError, ReviewInputError)):
                    fixture.invoke(run, args, result)
                action = fixture.action()
                self.assertEqual(action["phase"], "pending")
                self.assertTrue(action["execution_started"])
                self.assertIsNone(action["result"])
                calls = len(fixture.calls)
                with self.assertRaises(ReviewInputError):
                    fixture.invoke(run, args, ReplyResult("replied", comment_id=92))
                self.assertEqual(len(fixture.calls), calls)

    def test_service_receives_native_ledger_budget_and_explicit_operation_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ReplyFixture(directory)
            run, args = fixture.prepare()
            fixture.invoke(run, args, ReplyResult("replied", comment_id=91))
            call = fixture.calls[0]
            self.assertIs(call["evidence_budget"], run.caller.budget_view)
            self.assertIsInstance(call["budget"], ResourceBudget)
            self.assertEqual(call["operation_entry"], "enabled")
            self.assertIsInstance(call["operation_store"], GitHubCommentOperationStore)
            self.assertIsInstance(call["operation_parts"], ResultPartStore)
            port = call["operation_store"]._port
            self.assertIs(port._http, run.http)
            self.assertEqual(port._token, run.ledger.token)
            self.assertEqual(port._app_user_id, run.ledger._evidence_author_id)
            self.assertEqual(call["current_action"].run, run)
            self.assertTrue(call["options"].github_session_ledger)
            self.assertTrue(call["options"].complete_inventory)
            self.assertEqual(call["model"], run.plan.model)
            self.assertTrue(any(path.endswith("/tags") for _, _, path in fixture.trace))

    def test_unknown_native_business_write_cannot_be_closed_by_reply_status(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ReplyFixture(directory)
            run, args = fixture.prepare()
            def generate(**kwargs):
                with self.assertRaises(TimeoutError):
                    with run.caller.open(Request(
                        "https://api.github.test/synthetic-unknown-effect", data=b"{}", method="POST"
                    ), timeout=10) as response:
                        response.read(1)
                return fixture.synthetic_generic_completion(ReplyResult("replied", comment_id=91))
            with patch("review_sensei_github.action_reply.application", return_value=SimpleNamespace(
                generate_and_publish_reply=generate,
                reviewer=SimpleNamespace(finalizer=SimpleNamespace()),
            )), patch.dict(os.environ, {"GITHUB_RUN_ID": "456", "GITHUB_RUN_ATTEMPT": "1"}, clear=True):
                with self.assertRaises(ReviewInputError):
                    execute_reply(run, args)
            self.assertEqual(fixture.action()["phase"], "pending")
            self.assertGreater(run.caller.accounting.uncertain, 0)

    def test_source_change_conflict_prevents_provider_and_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ReplyFixture(directory)
            run, args = fixture.prepare()
            fixture.source["body"] = "changed after grant"
            with self.assertRaises(ReviewSenseiError):
                fixture.invoke(run, args, ReplyResult("replied", comment_id=91))
            self.assertEqual(fixture.calls, [])
            self.assertIsNone(fixture.action())

    def test_broader_review_service_and_policy_gates_are_retained(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ReplyFixture(directory)
            run, args = fixture.prepare()
            args.enable_broader_review = True
            configuration = load_configuration(run.configuration_path)
            unified = replace(configuration, advanced=replace(
                configuration.advanced, review_work=replace(configuration.advanced.review_work, mode="unified")
            ))
            provider, broader, recovery = _providers(run, args, unified)
            self.assertIsNotNone(provider)
            self.assertIsInstance(broader, ReviewService)
            self.assertIsNone(recovery)
            with self.assertRaises(ReviewInputError):
                _providers(run, args, configuration)

    def test_only_explicit_terminal_statuses_have_outcomes(self):
        for result in (
            ReplyResult("future_reply_status"), ReplyResult("replied"),
            ReplyResult("replied", comment_id=True),
            ReplyResult("replied", comment_id=91, assessment_status="broader_review_pending"),
            ReplyResult("replied", comment_id=91, approval_status="approved", approval_diagnostic="check_reconciliation_failed"),
        ):
            with self.subTest(result=result), self.assertRaises(ReviewInputError):
                _outcome(result)

    def test_provider_arguments_cannot_replace_frozen_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ReplyFixture(directory)
            run, args = fixture.prepare()
            configuration = load_configuration(run.configuration_path)
            for field, value in (
                ("provider", "openrouter"), ("model", "another-model"),
                ("base_url", "https://other.example.invalid/api"),
            ):
                selected = copy.copy(args)
                setattr(selected, field, value)
                with self.subTest(field=field), self.assertRaises(ReviewInputError):
                    _providers(run, selected, configuration)


if __name__ == "__main__":
    unittest.main()
