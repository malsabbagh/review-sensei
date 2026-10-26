"""Actual reply-run entry and native action/journal with fake external HTTP only."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from fake_github_http import json_response
from review_sensei_github.action_completion import require_reply_completion
from review_sensei_github.api_run_budget import CallerRunBudget
from review_sensei_github.operation_host import OPERATION_COMMENT_MARKER
from review_sensei_github.run_authority import REQUEST_KEYS, parse_authority
from review_sensei_github.session_ledger import parse_session_comment
from test_api_work_persistence import HEAD, IDENTITY
from test_review_run import EntryFixture, fixture_process_environment

from review_sensei.api_work import ApiRunAccounting
from review_sensei.errors import ProviderError
from review_sensei.models import ProviderResponse

BODY = "@reviewsensei explain the changed branch"
UPDATED = "2026-10-26T00:00:00Z"
GRANT = "sg1.9999999999999." + "A" * 43


class ReplyEntryFixture(EntryFixture):
    def __init__(self, directory, *, limit=512, runner_kind="hosted"):
        super().__init__(directory, limit=limit, runner_kind=runner_kind)
        self.prepare_reply()

    def prepare_reply(self):
        self.event_name = "issue_comment"
        self.source = {
            "id": 41,
            "body": BODY,
            "updated_at": UPDATED,
            "created_at": UPDATED,
            "user": {"id": 42, "login": "alice", "type": "User"},
            "author_association": "OWNER",
            "issue_url": "https://api.github.test/repos/owner/repo/issues/7",
        }
        self.extra_comments[41] = self.source
        self.reply_document = {
            "body": "The changed branch preserves the caller contract.",
            "resolve": False,
        }
        self.provider_failure = False
        self.provider_requests = []
        self.ambient_app_id = None
        self.reactions = []
        self.removed_reactions = []
        self.lost_reply = None
        self.reply_posts = 0
        self.cleanup_failure = False
        self.last_result = None
        self.thread_rows = None
        self.resolved_threads = []
        self.enable_broader_review = False
        self.provider_callback = None

    def broker_response(self, payload):
        if "session_grant" in payload:
            assert payload["session_grant"] == GRANT
            parsed = parse_authority(payload["session_attestation"], grant=True)
            assert parsed["purpose"] == "reply"
            assert (
                parsed["source"]["body_sha256"]
                == hashlib.sha256(self.source["body"].encode()).hexdigest()
            )
            return {
                "session_attestation": parsed,
                "control_receipt": {**payload["control_budget"], "dispatched": 1},
            }
        authority = parse_authority(payload["session_attestation"])
        assert set(authority) == REQUEST_KEYS and authority["purpose"] == "reply"
        assert authority["run_id"] == str(self.run_id)
        assert (
            authority["run_attempt"] == self.attempt
            and authority["job"] == self.owner_job
        )
        assert authority["repository"] == IDENTITY.repository
        assert authority["repository_id"] == IDENTITY.repository_id
        assert authority["pull_request"] == IDENTITY.pull_request
        assert authority["head_sha"] == self.live_head
        assert authority["source"] == {
            "kind": "issue_comment",
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

    def external(self, request, timeout):
        assert (
            len(self.accounted.observations)
            == sum(a == self.attempt for a, _, _ in self.trace) + 1
        )
        path = urlsplit(request.full_url).path
        method = request.get_method()
        if self.thread_rows is not None and path == "/graphql":
            self.trace.append((self.attempt, method, path))
            payload = json.loads(request.data)
            if payload.get("operationName") == "ResolveReviewThread":
                identifier = payload["variables"]["input"]["threadId"]
                row = next(row for row in self.thread_rows if row["id"] == identifier)
                row["isResolved"] = True
                self.resolved_threads.append(identifier)
                return json_response(
                    {
                        "data": {
                            "resolveReviewThread": {
                                "thread": {"id": identifier, "isResolved": True}
                            }
                        }
                    }
                )
            if payload.get("operationName") == "ResolutionReadback":
                row = next(
                    row
                    for row in self.thread_rows
                    if row["id"] == payload["variables"]["id"]
                )
                return json_response(
                    {
                        "data": {
                            "node": {"id": row["id"], "isResolved": row["isResolved"]}
                        }
                    }
                )
            return json_response(
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "reviewThreads": {
                                    "nodes": copy.deepcopy(self.thread_rows),
                                    "pageInfo": {
                                        "hasNextPage": False,
                                        "endCursor": None,
                                    },
                                }
                            }
                        }
                    }
                }
            )
        if self.thread_rows is not None and "/pulls/comments/" in path:
            self.trace.append((self.attempt, method, path))
            identifier = int(path.rsplit("/", 1)[1])
            return json_response(
                next(comment for comment in self.inline if comment["id"] == identifier)
            )
        if path.endswith("/contents/.github/review-sensei/learnings"):
            self.trace.append((self.attempt, method, path))
            return json_response({}, 404)
        if urlsplit(
            request.full_url
        ).hostname == "broker.github.test" and path.endswith("/session-grant"):
            self.trace.append((self.attempt, method, path))
            return json_response(self.broker_response(json.loads(request.data)))
        if "/reactions" in path:
            self.trace.append((self.attempt, method, path))
            if method == "POST":
                reaction = {
                    "id": 800 + len(self.reactions),
                    "content": "eyes",
                    "user": {"id": 999, "type": "Bot"},
                }
                self.reactions.append(reaction)
                return json_response(reaction, 201)
            self.removed_reactions.append(int(path.rsplit("/", 1)[1]))
            if self.cleanup_failure:
                raise TimeoutError("synthetic unknown reaction cleanup")
            return json_response({}, 204)
        body = json.loads(request.data) if request.data else {}
        is_reply = (
            method == "POST"
            and path.endswith("/issues/7/comments")
            and "reviewsensei:reply:" in body.get("body", "")
        )
        if is_reply:
            self.reply_posts += 1
            if self.lost_reply == "absent":
                self.trace.append((self.attempt, method, path))
                raise TimeoutError("synthetic unknown absent reply")
            if self.lost_reply == "stored":
                super().external(request, timeout)
                self.lost_reply = None
                raise TimeoutError("synthetic unknown stored reply")
        return super().external(request, timeout)

    def invoke_reply(self):
        from review_sensei_github import action_reply, action_run, review_run
        from review_sensei_github import cli as package_cli
        from review_sensei_github.api_run_requests import ApiRunRequestTransport
        from review_sensei_github.application import GitHubApplication
        from review_sensei_github.broker_client import BrokerClient
        from review_sensei_github.http import GitHubHttp

        from review_sensei.bounded_evidence import EvidenceReadBudget

        fixture = self
        event = self.root / "reply-event.json"
        event.write_text(
            json.dumps(
                {
                    "issue": {"number": 7},
                    "comment": self.source,
                    "repository": {"id": 123, "full_name": "owner/repo"},
                    "sender": self.source["user"],
                }
            )
        )
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
        if self.ambient_app_id is not None:
            environment["REVIEWSENSEI_APP_ID"] = self.ambient_app_id

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
        original_generate = GitHubApplication.generate_and_publish_reply

        def bootstrap(*args, **kwargs):
            run = original_bootstrap(*args, **kwargs)
            fixture.selected = run
            return run

        def generate(application, *args, **kwargs):
            result = original_generate(application, *args, **kwargs)
            fixture.last_result = result
            return result

        class Provider:
            name = "ollama"
            model = (
                "deepseek-v4.1-flash:cloud"
                if fixture.owner_job == "hosted"
                else "qwen3.5:4b"
            )

            def complete(self, request):
                fixture.provider_calls += 1
                fixture.provider_requests.append(request)
                if fixture.provider_failure:
                    raise ProviderError("synthetic completion unknown")
                return ProviderResponse(
                    text=json.dumps(
                        fixture.provider_callback(request)
                        if fixture.provider_callback
                        else fixture.reply_document
                    ),
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
                    patch.object(action_run, "bootstrap", side_effect=bootstrap),
                    patch.object(
                        GitHubApplication, "generate_and_publish_reply", generate
                    ),
                    patch.object(
                        action_reply, "default_registry", return_value=Registry()
                    ),
                    patch.object(
                        EvidenceReadBudget,
                        "__init__",
                        side_effect=AssertionError("new phase allowance"),
                    ),
                    redirect_stdout(output),
                    redirect_stderr(errors),
                ]:
                    stack.enter_context(context)
                backend = (
                    "cloud-ollama" if self.owner_job == "hosted" else "local-ollama"
                )
                model = (
                    "deepseek-v4.1-flash:cloud"
                    if self.owner_job == "hosted"
                    else "qwen3.5:4b"
                )
                base_url = (
                    "https://ollama.com/api"
                    if self.owner_job == "hosted"
                    else "http://127.0.0.1:11434/api"
                )
                result = package_cli.main(
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
                        str(self.source["id"]),
                        "--source-updated-at",
                        self.source["updated_at"],
                        "--source-kind",
                        "issue",
                        "--allow-write",
                        "--enable-reply",
                        "--generate",
                        "--config",
                        str(self.root / ".reviewsensei.yml"),
                        "--provider",
                        backend,
                        "--model",
                        model,
                        "--base-url",
                        base_url,
                        *(
                            ["--enable-broader-review"]
                            if fixture.enable_broader_review
                            else []
                        ),
                    ]
                )
            self.error, self.output = errors.getvalue(), output.getvalue()
            return result
        finally:
            os.chdir(old)

    def action(self):
        return parse_session_comment(
            self.comment["body"], identity=IDENTITY
        ).api_run_accounting["action"]


class ActionReplyEntryTests(unittest.TestCase):
    def test_generic_reply_runs_actual_provider_journal_publication_cleanup_and_terminal(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ReplyEntryFixture(directory)
            self.assertEqual(fixture.invoke_reply(), 0, fixture.error)
            self.assertEqual(fixture.provider_calls, 1)
            self.assertEqual(fixture.action()["phase"], "terminal")
            self.assertIsInstance(fixture.selected.caller.budget_view, CallerRunBudget)
            self.assertEqual(len(fixture.reactions), 1)
            self.assertEqual(fixture.removed_reactions, [fixture.reactions[0]["id"]])
            self.assertEqual(fixture.reply_posts, 1)
            self.assertIsNone(fixture.ambient_app_id)
            self.assertEqual(fixture.selected.ledger.authenticated_app_id, 999)
            self.assertTrue(
                any(
                    OPERATION_COMMENT_MARKER in comment["body"]
                    for comment in fixture.extra_comments.values()
                )
            )
            self.assertEqual(len(fixture.accounted.observations), len(fixture.trace))
            self.assertTrue(
                require_reply_completion(
                    fixture.last_result,
                    fixture.selected.session_authority,
                    run_id=str(fixture.run_id),
                    run_attempt=str(fixture.attempt),
                )
            )

    def test_local_model_discovery_uses_the_same_physical_caller_counter(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ReplyEntryFixture(directory, runner_kind="local")
            self.assertEqual(fixture.invoke_reply(), 0, fixture.error)
            self.assertEqual(fixture.provider_calls, 1)
            self.assertEqual(
                sum(path == "/api/tags" for _, _, path in fixture.trace), 1
            )
            self.assertEqual(len(fixture.accounted.observations), len(fixture.trace))
            self.assertEqual(fixture.action()["phase"], "terminal")

    def test_provider_unknown_keeps_action_pending_and_never_reinfers_on_stopped_successor(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ReplyEntryFixture(directory)
            fixture.provider_failure = True
            self.assertEqual(fixture.invoke_reply(), 1)
            self.assertEqual(fixture.provider_calls, 1)
            self.assertEqual(fixture.action()["phase"], "pending")
            self.assertTrue(fixture.action()["execution_started"])
            self.assertEqual(fixture.reply_posts, 0)
            fixture.attempt = 2
            fixture.provider_failure = False
            self.assertEqual(fixture.invoke_reply(), 1)
            self.assertEqual(fixture.provider_calls, 1)
            self.assertEqual(fixture.reply_posts, 0)

    def test_lost_reply_response_preserves_charge_and_does_not_duplicate(self):
        for loss in ("stored", "absent"):
            with self.subTest(loss=loss), tempfile.TemporaryDirectory() as directory:
                fixture = ReplyEntryFixture(directory)
                fixture.lost_reply = loss
                result = fixture.invoke_reply()
                self.assertIn(result, (0, 1))
                self.assertEqual(fixture.provider_calls, 1)
                self.assertEqual(fixture.reply_posts, 1)
                self.assertGreater(fixture.accounted.accounting.uncertain, 0)
                if result == 0:
                    self.assertEqual(loss, "stored")
                    self.assertEqual(fixture.action()["phase"], "terminal")
                    self.assertTrue(
                        require_reply_completion(
                            fixture.last_result,
                            fixture.selected.session_authority,
                            run_id=str(fixture.run_id),
                            run_attempt=str(fixture.attempt),
                        )
                    )
                else:
                    self.assertEqual(fixture.action()["phase"], "pending")
                if loss == "absent":
                    self.assertEqual(result, 1)
                    self.assertEqual(fixture.action()["phase"], "pending")
                fixture.attempt = 2
                self.assertEqual(fixture.invoke_reply(), 1)
                self.assertEqual(fixture.provider_calls, 1)
                self.assertEqual(fixture.reply_posts, 1)

    def test_unknown_cleanup_preserves_pending_action_after_one_visible_reply(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ReplyEntryFixture(directory)
            fixture.cleanup_failure = True
            self.assertEqual(fixture.invoke_reply(), 1)
            self.assertEqual(fixture.reply_posts, 1)
            self.assertEqual(fixture.action()["phase"], "pending")
            self.assertGreater(fixture.accounted.accounting.uncertain, 0)
            fixture.attempt = 2
            fixture.cleanup_failure = False
            self.assertEqual(fixture.invoke_reply(), 1)
            self.assertEqual(fixture.provider_calls, 1)
            self.assertEqual(fixture.reply_posts, 1)

    def test_overlapping_writer_refuses_before_provider_or_business_effect(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ReplyEntryFixture(directory)
            fixture.overlap = True
            self.assertEqual(fixture.invoke_reply(), 1)
            self.assertEqual(fixture.provider_calls, 0)
            self.assertEqual(fixture.reply_posts, 0)
            self.assertIsNone(fixture.comment)

    def test_human_reassessment_keeps_twenty_retained_decisions_and_finishes_known_withholding(
        self,
    ):
        from test_review_run_rescan import partial_fixture

        with tempfile.TemporaryDirectory() as directory:
            prior, _transaction = partial_fixture(directory)
            original = parse_session_comment(prior.comment["body"], identity=IDENTITY)
            fixture = ReplyEntryFixture.__new__(ReplyEntryFixture)
            fixture.__dict__.update(prior.__dict__)
            fixture.next_run()
            fixture.live_head = fixture.requested_head = HEAD
            fixture.prepare_reply()
            fixture.thread_rows = [
                {
                    "id": "native-thread-" + str(comment["id"]),
                    "isResolved": True,
                    "comments": {
                        "nodes": [
                            {
                                "databaseId": comment["id"],
                                "body": comment["body"],
                                "path": comment["path"],
                                "line": comment["line"],
                                "author": {
                                    "login": "reviewsensei",
                                    "__typename": "Bot",
                                },
                            }
                        ]
                    },
                }
                for comment in fixture.inline
            ]
            fixture.source["body"] = (
                "@reviewsensei The caller contract is intentional; the added change is checked."
            )
            fixture.reply_document = {
                "body": "The explanation is supported by the current diff.",
                "assessments": [
                    {
                        "fingerprint": decision["fingerprint"],
                        "decision": "dismissed",
                        "rationale": "The current diff contains the intended added change.",
                        "human_evidence": "The caller contract is intentional",
                        "diff_evidence": "@@ -1 +1,2 @@\n keep\n+change",
                    }
                    for decision in original.dispositions
                ],
            }
            before = fixture.provider_calls
            self.assertEqual(fixture.invoke_reply(), 0, fixture.error)
            self.assertEqual(fixture.provider_calls, before + 1)
            self.assertEqual(len(fixture.provider_requests), 1)
            for decision in original.dispositions:
                self.assertIn(
                    decision["fingerprint"], fixture.provider_requests[0].prompt
                )
            current = parse_session_comment(fixture.comment["body"], identity=IDENTITY)
            self.assertEqual(current.dispositions, original.dispositions)
            self.assertEqual(len(current.dispositions), 20)
            self.assertEqual(
                current.transaction.to_dict(), original.transaction.to_dict()
            )
            self.assertEqual(fixture.action()["phase"], "terminal")
            self.assertGreaterEqual(
                ApiRunAccounting.from_document(current.api_run_accounting).remaining, 0
            )
            self.assertEqual(
                fixture.last_result.approval_diagnostic, "review_incomplete"
            )
            self.assertEqual(fixture.last_result.resolution_status, "resolved")
            self.assertTrue(
                require_reply_completion(
                    fixture.last_result,
                    fixture.selected.session_authority,
                    run_id=str(fixture.run_id),
                    run_attempt=str(fixture.attempt),
                )
            )
            self.assertFalse(
                any(review["state"] == "APPROVED" for review in fixture.reviews)
            )

    def test_broader_publication_handoff_cannot_reuse_prior_human_completion(self):
        from review_sensei.human_assessment import finding_instance_fingerprint
        from review_sensei.models import ReviewResult

        with tempfile.TemporaryDirectory() as directory:
            prior = EntryFixture(directory)
            prior.config_bytes += b"advanced:\n  review_work:\n    mode: unified\n"
            (prior.root / ".reviewsensei.yml").write_bytes(prior.config_bytes)
            subprocess.run(
                ["git", "-C", str(prior.root), "add", ".reviewsensei.yml"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(prior.root),
                    "-c",
                    "user.name=Fixture",
                    "-c",
                    "user.email=fixture@example.invalid",
                    "commit",
                    "-m",
                    "unified fixture policy",
                ],
                check=True,
                capture_output=True,
            )
            prior.policy = prior.policy_ref = subprocess.check_output(
                ["git", "-C", str(prior.root), "rev-parse", "HEAD"], text=True
            ).strip()
            self.assertEqual(prior.invoke(human=True), 0, prior.error)
            original_result = ReviewResult.from_dict(
                json.loads((prior.root / "review.json").read_text())
            )
            fingerprint = finding_instance_fingerprint(original_result.comments[0])
            fixture = ReplyEntryFixture.__new__(ReplyEntryFixture)
            fixture.__dict__.update(prior.__dict__)
            fixture.next_run()
            fixture.live_head = fixture.requested_head = HEAD
            fixture.prepare_reply()
            fixture.enable_broader_review = True
            fixture.source["body"] = (
                "@reviewsensei Please inspect the wider caller context before deciding."
            )

            def response(request):
                if request.prompt.startswith("Reassess the pending ReviewSensei"):
                    return {
                        "body": "Broader inspection is needed.",
                        "assessments": [],
                        "context_requests": [
                            {
                                "reference": fingerprint,
                                "kind": "discovery",
                                "required_paths": ["src/app.py"],
                                "reason": "Inspect the complete caller context.",
                            }
                        ],
                    }
                return {"summary": "Broader inspection is complete.", "comments": []}

            fixture.provider_callback = response
            before = fixture.provider_calls
            self.assertEqual(fixture.invoke_reply(), 1, fixture.error)
            self.assertGreater(fixture.provider_calls, before + 1)
            self.assertEqual(
                fixture.last_result.assessment_status, "broader_review_handoff"
            )
            self.assertEqual(
                fixture.last_result.assessment_diagnostic,
                "durable_baseline_recovery_required",
            )
            self.assertEqual(fixture.action()["phase"], "pending")
            self.assertIsNone(fixture.last_result.finalization_completion)
            self.assertFalse(
                require_reply_completion(
                    fixture.last_result,
                    fixture.selected.session_authority,
                    run_id=str(fixture.run_id),
                    run_attempt=str(fixture.attempt),
                )
            )
            self.assertFalse(
                any(review["state"] == "APPROVED" for review in fixture.reviews)
            )
            completed = fixture.provider_calls
            fixture.attempt = 2
            self.assertEqual(fixture.invoke_reply(), 1)
            self.assertEqual(fixture.provider_calls, completed)
