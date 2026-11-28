"""Offline coverage for the installed caller's read-only and coordinated entry points."""

from __future__ import annotations

import copy
import io
import json
import os
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from review_sensei_github.checks import required_check_identity
from review_sensei_github.http import GitHubHttp
from review_sensei_github.v6_coordinator_client import V6CoordinatorClient
from review_sensei_github.v6_credential_matrix import GitHubEventKind
from review_sensei_github.v6_fresh_analysis import (
    V6FreshAnalysisContext,
    V6PlannedBatch,
)
from review_sensei_github.v6_pilot_runner import (
    V6PilotRunConfig,
    V6PilotRunner,
    run_v6_pilot,
    run_v6_pilot_cli,
)
from review_sensei_github.v6_recovery_ledger import extract_v6_record_markers
from review_sensei_github.v6_workflow_event import plan_workflow

from review_sensei.errors import ReviewInputError
from review_sensei.models import ProviderResponse, ReviewResult
from tests.test_v6_one_writer_runner import (
    APP_ID,
    HEAD_A,
    OWNER,
    PR_NUMBER,
    REPO,
    REPO_ID,
    WORKFLOW_SHA,
    _app_meta,
    _ScriptedTransport,
    _SimulatedCoordinatorStore,
    _SimulatedPRSurface,
)


class ReadOnlyAnalysisTests(unittest.TestCase):
    def test_installed_writes_off_reviews_in_summary_without_a_writer(self):
        head = "a" * 40
        batch = V6PlannedBatch(
            "b0", "@@ -1 +1 @@\n-old\n+new", "b" * 64, 32, ("a.py",), ("h0",), ()
        )
        service = SimpleNamespace(
            review=Mock(
                return_value=ReviewResult(
                    summary="The return value changes",
                    comments=(),
                    provider="fixture",
                    model="fixture",
                    review_status="complete",
                )
            )
        )
        context = V6FreshAnalysisContext(
            owner="octo",
            repo="demo",
            pull_request=5,
            pr_title="Change return",
            base_sha="b" * 40,
            merge_base_sha="b" * 40,
            head_sha=head,
            inventory_sha256="c" * 64,
            inventory=(),
            non_text_changes=(),
            policy_digest="d" * 64,
            configuration_digest="e" * 64,
            max_provider_calls=1,
            planned_batches=(batch,),
            all_expected_hunk_ids=("h0",),
            sub_hunks_by_parent={},
            combined_diff_sha256="f" * 64,
            provider_name="fixture",
            model_name="fixture",
            service=service,
        )
        coordinator = Mock(spec=["status"])
        coordinator.status.return_value = SimpleNamespace(
            state="IDLE", slot_key="fixture"
        )
        workflow = Mock(
            spec=["request"],
            request=Mock(side_effect=AssertionError("unexpected transport")),
        )
        factory = Mock(side_effect=AssertionError("no writer credential"))
        runner = V6PilotRunner(
            coordinator=coordinator,
            workflow_token_transport=workflow,
            app_transport_factory=factory,
        )
        with tempfile.TemporaryDirectory() as tmp:
            policy = Path(tmp)
            (policy / ".reviewsensei.yml").write_text(
                "schema: 1\ngithub:\n  writes: false\n  automatic_reviews: true\n"
            )
            summary = []
            verify = Mock()
            config = V6PilotRunConfig(
                owner="octo",
                repo="demo",
                repository_id=1,
                pull_request=5,
                head_sha=head,
                operation_id="op",
                analyze_fresh_pr=True,
                policy_root=policy,
                fresh_analysis_context_loader=lambda: context,
                pre_publish_head_verifier=verify,
                step_summary_sink=summary.append,
            )
            outcome = runner.run(config)
            self.assertEqual(outcome.status, "analyzed")
            self.assertEqual(outcome.model_calls_started, 1)
            self.assertIn("The return value changes", "".join(summary))
            self.assertEqual(
                coordinator.status.call_args.kwargs["analysis_head_sha"], head
            )
            factory.assert_not_called()
            workflow.request.assert_not_called()
            self.assertEqual(verify.call_count, 2)
            (policy / ".reviewsensei.yml").write_text(
                "schema: 1\ngithub:\n  writes: false\n  automatic_reviews: false\n"
            )
            self.assertEqual(
                runner.run(config).diagnostic, "v6_policy_automatic_reviews_disabled"
            )
            self.assertEqual(
                runner.run(
                    replace(config, event=GitHubEventKind.WORKFLOW_DISPATCH)
                ).status,
                "analyzed",
            )
            self.assertEqual(service.review.call_count, 2)
            coordinator.status.return_value.state = "HOLD"
            self.assertEqual(
                runner.run(
                    replace(config, event=GitHubEventKind.WORKFLOW_DISPATCH)
                ).status,
                "hold",
            )
            self.assertEqual(service.review.call_count, 2)


class InstalledWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.sandbox = ExitStack()
        self.addCleanup(self.sandbox.close)
        self.tmp = Path(self.sandbox.enter_context(tempfile.TemporaryDirectory()))
        self.slug = f"{OWNER}/{REPO}"
        self.repo = {
            "full_name": self.slug,
            "id": REPO_ID,
            "fork": False,
            "default_branch": "main",
        }
        self.pull = {
            "state": "open",
            "number": PR_NUMBER,
            "draft": False,
            "title": "Change return",
            "base": {"repo": self.repo, "sha": "b" * 40, "ref": "main"},
            "head": {"repo": self.repo, "sha": HEAD_A, "ref": "feature"},
        }
        self.actor = {"type": "User", "id": 42, "login": "maintainer"}
        self.source = {
            "id": 18,
            "updated_at": "2026-01-02T04:00:00Z",
            "created_at": "2026-01-02T04:00:00Z",
            "body": "@reviewsensei explain this return",
            "user": self.actor,
            "author_association": "MEMBER",
            "issue_url": f"https://api.github.com/repos/{self.slug}/issues/{PR_NUMBER}",
            "pull_request_url": f"https://api.github.com/repos/{self.slug}/pulls/{PR_NUMBER}",
            "diff_hunk": "@@ -1 +1 @@\n-old\n+new",
            "path": "file.py",
            "in_reply_to_id": 17,
        }
        self.root = {
            **self.source,
            "id": 17,
            "body": "What changed here?",
            "in_reply_to_id": None,
        }
        self.http = GitHubHttp(opener=Mock(side_effect=AssertionError("network")))
        self.http.request = Mock(side_effect=self.get)
        self.http.paginate = Mock(side_effect=self.paginate)
        self.pr = _SimulatedPRSurface()
        self.now = 1_764_200_000.0
        self.store = _SimulatedCoordinatorStore(clock=lambda: self.now, pr=self.pr)
        self.coordinator = V6CoordinatorClient(
            base_url="https://coordinator.invalid",
            oidc_token_provider=lambda: "fixture",
            opener=self.store.opener,
            clock=lambda: self.now,
        )
        self.writes = []
        self.lose_reply = False
        self.alter_reply = False
        self.provider = SimpleNamespace(
            name="fixture",
            model="fixture",
            complete=Mock(
                return_value=ProviderResponse(
                    text='{"body":"It returns the updated value.","resolve":false}',
                    provider="fixture",
                )
            ),
        )
        self.sandbox.enter_context(
            patch(
                "review_sensei_github.v6_workflow_event.workflow_reader",
                return_value=self.http,
            )
        )
        self.sandbox.enter_context(
            patch(
                "review_sensei_github.v6_pilot_runner.V6CoordinatorClient",
                return_value=self.coordinator,
            )
        )
        self.sandbox.enter_context(
            patch(
                "review_sensei_github.v6_pilot_runner.run_v6_pilot",
                side_effect=lambda cfg, **kw: run_v6_pilot(
                    cfg,
                    coordinator=self.coordinator,
                    workflow_token_transport=_ScriptedTransport(self.pr.handle_wf),
                    app_transport_factory=lambda _: _ScriptedTransport(self.app),
                    clock=lambda: self.now,
                ),
            )
        )
        registry = self.sandbox.enter_context(
            patch("review_sensei_github.v6_workflow_event.default_registry")
        )
        registry.return_value.create.return_value = self.provider
        self.sandbox.enter_context(
            patch.dict(
                os.environ,
                {
                    "GITHUB_TOKEN": "fixture",
                    "GITHUB_ACTOR": "maintainer",
                    "GITHUB_TRIGGERING_ACTOR": "maintainer",
                    "GITHUB_ACTOR_ID": "42",
                    "GITHUB_REPOSITORY": self.slug,
                    "GITHUB_REPOSITORY_ID": str(REPO_ID),
                    "GITHUB_RUN_ID": "5002",
                    "GITHUB_RUN_ATTEMPT": "1",
                    "GITHUB_API_URL": "https://api.github.com",
                },
                clear=True,
            )
        )
        (self.tmp / ".reviewsensei.yml").write_text(
            "schema: 1\ngithub:\n  writes: true\n", encoding="utf-8"
        )

    def get(self, method, path, *, token):
        self.assertEqual(method, "GET")
        self.assertEqual(token, "fixture")
        if path.endswith(f"/pulls/{PR_NUMBER}"):
            return 200, self.pull
        if path.endswith("/collaborators/maintainer/permission"):
            return 200, {"permission": "write", "user": self.actor}
        if path.endswith("/comments/18"):
            return (200, self.source) if self.source is not None else (404, None)
        if path.endswith("/comments/17"):
            return 200, self.root
        if "/contents/" in path:
            return 404, None
        self.fail(path)

    def paginate(self, *, path, token, **kwargs):
        if path.endswith("/files"):
            return [{"filename": "file.py", "patch": "@@ -1 +1 @@\n-old\n+new"}]
        if path.endswith("/comments"):
            return [self.root, self.source]
        self.fail(path)

    def app(self, call):
        path, method, data = call["path"], call["method"], call.get("body") or {}
        if method == "GET" and path.endswith("/pulls/comments/800"):
            return 200, {}, self.pr.review_comments[0]
        if (
            method == "POST"
            and not path.endswith("/reviews")
            and "<!-- reviewsensei:v6:effect " in data.get("body", "")
        ):
            self.assertEqual(set(data), {"body"})
            self.writes.append(path)
            if path.endswith("/comments/17/replies"):
                row = {
                    **_app_meta(),
                    "id": 800,
                    "in_reply_to_id": 17,
                    "body": data["body"],
                    "commit_id": "c" * 40,
                }
                self.pr.review_comments.append(row)
            else:
                self.assertEqual(
                    path, f"/repos/{self.slug}/issues/{PR_NUMBER}/comments"
                )
                row = self.pr.add_comment(data["body"])
            if self.alter_reply:
                row["body"] = "changed\n" + row["body"]
            if self.lose_reply:
                raise TimeoutError("lost fixture response")
            return 201, {}, row
        return self.pr.handle_app(call)

    def event(self, kind):
        event = {"repository": self.repo, "sender": self.actor}
        if kind == "workflow_dispatch":
            event["inputs"] = {
                "operation": "review",
                "pull_request_number": str(PR_NUMBER),
                "review_remaining": "false",
            }
        elif kind == "issue_comment":
            event.update(
                action="created",
                comment=self.source,
                issue={"number": PR_NUMBER, "pull_request": {}},
            )
        elif kind == "pull_request_review_comment":
            event.update(action="created", comment=self.source, pull_request=self.pull)
        else:
            event.update(action="opened", pull_request=self.pull)
        return copy.deepcopy(event)

    def invoke(self, kind, payload=None, *, plan=False):
        path = self.tmp / "event.json"
        path.write_text(
            json.dumps(payload if payload is not None else self.event(kind)),
            encoding="utf-8",
        )
        ref = (
            f"refs/pull/{PR_NUMBER}/merge"
            if kind in {"pull_request", "pull_request_review_comment"}
            else "refs/heads/main"
        )
        head = HEAD_A if ref.endswith("/merge") else ""
        env = {
            "GITHUB_EVENT_NAME": kind,
            "GITHUB_EVENT_PATH": str(path),
            "GITHUB_REF": ref,
            "GITHUB_OUTPUT": str(self.tmp / "output"),
            "EVENT_KIND": kind,
            "PULL_REQUEST": str(PR_NUMBER),
            "HEAD_SHA": head,
            "REPOSITORY": self.slug,
            "REPOSITORY_ID": str(REPO_ID),
            "POLICY_ROOT": str(self.tmp),
        }
        with patch.dict(os.environ, env), redirect_stdout(io.StringIO()):
            if plan:
                plan_workflow()
                return (self.tmp / "output").read_text()
            run_v6_pilot_cli(
                [
                    "--repository",
                    self.slug,
                    "--repository-id",
                    str(REPO_ID),
                    "--pull-request",
                    str(PR_NUMBER),
                    "--workflow-sha",
                    WORKFLOW_SHA,
                    "--event",
                    kind,
                    "--head-sha",
                    head,
                    "--analyze-fresh-pr",
                    "--workflow-event-path",
                    str(path),
                    "--policy-root",
                    str(self.tmp),
                    "--outcome",
                    str(self.tmp / "result.json"),
                    "--step-summary-path",
                    str(self.tmp / "summary"),
                ]
            )
        return json.loads((self.tmp / "result.json").read_text())

    def check_mention(self, kind):
        result = self.invoke(kind)
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["coordinator_state"], "IDLE")
        self.assertTrue(result["lease_released"])
        self.assertEqual(result["model_calls_started"], 1)
        self.assertEqual(len(self.writes), 1)
        self.assertFalse(self.pr.reviews)
        self.assertEqual(self.provider.complete.call_count, 1)
        self.assertIn(
            "explain this return", self.provider.complete.call_args.args[0].prompt
        )
        schemas = [
            r.schema
            for row in self.pr.comments
            for r in extract_v6_record_markers(row["body"], strict=True)
        ]
        self.assertEqual(
            [s.split(":")[2] for s in schemas],
            ["owner", "unresolved", "intent", "receipt"],
        )
        rerun = self.invoke(kind)
        self.assertEqual(rerun["status"], "completed", rerun)
        self.assertEqual(rerun["model_calls_started"], 0)
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(self.provider.complete.call_count, 1)

    def test_manual_review_and_explicit_rescan_each_have_one_writer(self):
        batch = V6PlannedBatch(
            "batch-000",
            "@@ -1 +1 @@\n-old\n+new",
            "b" * 64,
            32,
            ("file.py",),
            ("h0",),
            ("h0",),
        )
        service = SimpleNamespace(
            review=Mock(
                return_value=ReviewResult(
                    summary="Check this return",
                    comments=(),
                    provider="fixture",
                    model="fixture",
                    review_status="complete",
                )
            )
        )
        context = V6FreshAnalysisContext(
            owner=OWNER,
            repo=REPO,
            pull_request=PR_NUMBER,
            pr_title="Change",
            base_sha="b" * 40,
            merge_base_sha="b" * 40,
            head_sha=HEAD_A,
            inventory_sha256="c" * 64,
            inventory=(
                {
                    "path": "file.py",
                    "status": "modified",
                    "disposition": "text",
                    "hunk_ids": ["h0"],
                },
            ),
            non_text_changes=(),
            policy_digest="d" * 64,
            configuration_digest="e" * 64,
            max_provider_calls=2,
            planned_batches=(batch,),
            all_expected_hunk_ids=("h0",),
            sub_hunks_by_parent={},
            combined_diff_sha256="f" * 64,
            provider_name="fixture",
            model_name="fixture",
            service=service,
        )
        with patch(
            "review_sensei_github.v6_pilot_runner.prepare_v6_fresh_analysis_context",
            return_value=context,
        ):
            for kind in ("workflow_dispatch", "issue_comment"):
                with (
                    self.subTest(kind=kind),
                    patch.dict(self.source, {"body": "@sensei rescan"}),
                ):
                    before = len(self.pr.reviews)
                    result = self.invoke(kind)
                    self.assertEqual(result["status"], "completed", result)
                    self.assertEqual(result["coordinator_state"], "IDLE")
                    self.assertEqual(self.invoke(kind)["model_calls_started"], 0)
                    self.assertEqual(len(self.pr.reviews), before + 1)
        self.assertEqual(service.review.call_count, 2)

        # A repository can require a context from one specific GitHub App.
        # Exercise the check runs left by the actual review above as well as
        # a successful same-name Actions job and a stale ReviewSensei check.
        context = required_check_identity(app_slug="reviewsensei[bot]")["name"]
        required = {"context": "ReviewSensei", "app_id": APP_ID}
        expected = (required["context"], required["app_id"], HEAD_A, "success")

        def identities(runs):
            return {
                (
                    r.get("name"),
                    r.get("app", {}).get("id"),
                    r.get("head_sha"),
                    r.get("conclusion"),
                )
                for r in runs
            }

        self.assertNotIn(expected, identities(self.pr.check_runs))
        good = {
            "name": context,
            "app": {"id": APP_ID},
            "head_sha": HEAD_A,
            "conclusion": "success",
        }
        self.assertIn(expected, identities([good]))
        self.assertNotIn(
            expected,
            identities(
                [
                    {**good, "app": {"id": APP_ID + 1}},
                    {**good, "head_sha": "9" * 40},
                    {**good, "name": "ReviewSensei / review"},
                    {**good, "conclusion": "failure"},
                ]
            ),
        )

    def test_manual_rescan_requires_current_maintainer_on_original_event(self):
        payload = self.event("workflow_dispatch")
        with (
            patch.dict(os.environ, {"GITHUB_TRIGGERING_ACTOR": "someone-else"}),
            self.assertRaisesRegex(ReviewInputError, "actor_invalid"),
        ):
            self.invoke("workflow_dispatch", payload)
        self.http.request.side_effect = lambda method, path, **kwargs: (
            (200, {"permission": "read", "user": self.actor})
            if path.endswith("/permission")
            else self.get(method, path, **kwargs)
        )
        with self.assertRaisesRegex(ReviewInputError, "permission_unverified"):
            self.invoke("workflow_dispatch", payload)
        self.assertEqual(self.pr.comments, [])

    def test_writes_off_and_mentions_off_for_actual_comment_events(self):
        config = self.tmp / ".reviewsensei.yml"
        config.write_text("schema: 1\ngithub:\n  writes: false\n", encoding="utf-8")
        status = SimpleNamespace(state="IDLE", slot_key="fixture")
        with (
            patch.object(self.coordinator, "status", return_value=status) as check,
            patch.object(
                self.coordinator, "acquire", side_effect=AssertionError("no lease")
            ),
        ):
            for event in ("issue_comment", "pull_request_review_comment"):
                result = self.invoke(event)
                self.assertEqual(result["status"], "analyzed", result)
                self.assertEqual(result["model_calls_started"], 1)
                self.assertEqual(check.call_args.kwargs["analysis_head_sha"], HEAD_A)
                self.assertEqual(self.pr.comments, [])
            self.assertIn("updated value", (self.tmp / "summary").read_text())
            config.write_text("schema: 1\ngithub:\n  writes: true\n  mentions: false\n")
            self.assertEqual(
                self.invoke("issue_comment")["diagnostic"],
                "v6_policy_mentions_disabled",
            )
            self.assertEqual(self.provider.complete.call_count, 2)

    def test_top_level_mention_and_replay(self):
        self.check_mention("issue_comment")

    def test_inline_reply_to_root_reconciles_lost_response_and_replay(self):
        self.lose_reply = True
        self.check_mention("pull_request_review_comment")
        self.assertTrue(self.writes[0].endswith("/comments/17/replies"))

    def test_edited_landed_inline_reply_is_held_without_resend(self):
        self.lose_reply = self.alter_reply = True
        result = self.invoke("pull_request_review_comment")
        self.assertEqual(result["status"], "hold", result)
        self.assertEqual(len(self.writes), 1)

    def test_fork_manual_and_inline_refused_before_coordinator(self):
        events = {
            kind: self.event(kind)
            for kind in (
                "workflow_dispatch",
                "issue_comment",
                "pull_request_review_comment",
            )
        }
        self.pull["head"]["repo"] = {**self.repo, "id": 99, "fork": True}
        for kind, event in events.items():
            with (
                self.subTest(kind=kind),
                self.assertRaisesRegex(
                    ReviewInputError, "v6_untrusted_head_repository"
                ),
            ):
                self.invoke(kind, event)
        self.provider.complete.assert_not_called()
        self.assertEqual(self.pr.comments, [])

    def test_missing_edited_cross_pr_bot_and_wrong_root_fail_closed(self):
        for key, value in [
            ("updated_at", "2026-01-03T00:00:00Z"),
            ("body", "@sensei changed"),
            ("id", 99),
            ("user", {**self.actor, "id": 55}),
            ("user", {**self.actor, "type": "Bot"}),
            ("author_association", "NONE"),
            ("in_reply_to_id", 77),
            ("pull_request_url", "different"),
        ]:
            payload = self.event("pull_request_review_comment")
            with (
                self.subTest(key=key),
                patch.dict(self.source, {key: value}),
                self.assertRaises(ReviewInputError),
            ):
                self.invoke("pull_request_review_comment", payload)
        payload = self.event("issue_comment")
        self.source = None
        with self.assertRaisesRegex(ReviewInputError, "source_gone"):
            self.invoke("issue_comment", payload)
        self.provider.complete.assert_not_called()
        self.assertEqual(self.pr.comments, [])

    def test_source_edit_after_analysis_creates_no_native_write(self):
        def edit_source(_req):
            self.source["body"] = "@sensei revised request"
            return ProviderResponse(
                '{"body":"A reply","resolve":false}', provider="fixture"
            )

        self.provider.complete.side_effect = edit_source
        result = self.invoke("issue_comment")
        self.assertEqual(result["status"], "hold")
        self.assertEqual(self.writes, [])
        self.assertFalse(self.pr.reviews)

    def test_hosted_plan_pins_manual_head_and_preserves_local_backend(self):
        self.assertIn(f"head_sha={HEAD_A}", self.invoke("workflow_dispatch", plan=True))
        self.assertIn(
            "runner_kind=local",
            (self.tmp / "output").read_text(),
        )
        payload = self.event("workflow_dispatch")
        payload["inputs"]["review_remaining"] = "true"
        with self.assertRaisesRegex(
            ReviewInputError, "remaining_requires_retained_coverage"
        ):
            self.invoke("workflow_dispatch", payload)
        for event in ("issue_comment", "pull_request_review_comment"):
            with (
                self.subTest(event=event),
                patch.dict(self.source, {"body": "@sensei review pause"}),
                self.assertRaisesRegex(ReviewInputError, "command_requires_migration"),
            ):
                self.invoke(event)
        self.provider.complete.assert_not_called()
        self.assertEqual(self.writes, [])
