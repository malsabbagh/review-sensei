"""Actual CLI publication/recovery over serialized ledger and synthetic APIs.

Installed-compatible fixture closure: tests.test_publication_recovery imports
fake_github_http, test_human_assessment and test_review_transaction. No provider,
application, publisher, accepted-result guard or ledger method is replaced.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import review_sensei_github as github
from review_sensei_github.cli import main as github_main
from review_sensei_github.operation_comments import (
    GitHubIssueCommentOperationPort,
    ResultPartStore,
)
from review_sensei_github.operation_entry import (
    HostedProviderResult,
    run_hosted_provider,
)
from review_sensei_github.operation_host import (
    RESULT_COMMENT_MARKER,
    GitHubCommentOperationStore,
)
from review_sensei_github.publication import (
    ReviewApprovalFinalizer,
    parse_finding_index,
)
from review_sensei_github.session_ledger import (
    GitHubIssueCommentSessionLedger,
    render_session_comment,
)

import review_sensei.cli as engine_cli
from review_sensei.baseline import admission_context_document
from review_sensei.context import finding_lifecycle_for_comment
from review_sensei.human_assessment import finding_instance_fingerprint
from review_sensei.service import ReviewService
from review_sensei.session import LocalSessionLedger
from tests.fake_github_http import json_response
from tests.test_publication_recovery import (
    APP,
    BASE,
    CONFIGURATION,
    DIFF,
    EVIDENCE,
    HEAD,
    IDENTITY,
    RetentionState,
    _Broker,
    checkpoint,
)

PR342_COMMENT_SIZES = (
    6932,
    59998,
    59998,
    670,
    1228,
    46978,
    59998,
    59998,
    18750,
    1228,
    59998,
    59998,
    3694,
    59998,
    59998,
    25098,
    1228,
    59998,
    59998,
    17078,
)


class _HostedBroker(_Broker):
    def open_session(self, *args, **kwargs):
        return SimpleNamespace(
            token="synthetic-session",
            state="known",
            reservation_owner=None,
        )


class JourneyState(RetentionState):
    """Add canonical individual operation-comment GET/PATCH to the fixture API."""

    def open(self, request, timeout):
        path = urlparse(request.full_url).path
        if "/issues/comments/" in path:
            identifier = path.rsplit("/", 1)[-1]
            item = next((p for p in self.parts if str(p["id"]) == identifier), None)
            if item is not None:
                self.calls.append((request.method, path, None))
                if request.method == "PATCH":
                    item["body"] = json.loads(request.data)["body"]
                if request.method in {"GET", "PATCH"}:
                    return json_response(item)
        return super().open(request, timeout)


class PR342JourneyState(JourneyState):
    """Sanitized PR #342 comment distribution where page_size >= 20 exceeds 512 KiB."""

    def __init__(self, record):
        super().__init__()
        body0 = render_session_comment(repository_id=1, pull_request=1, record=record)
        self.session_comment = {
            "id": 600,
            "body": body0
            + "\n"
            + ("x" * max(0, PR342_COMMENT_SIZES[0] - len(body0) - 1)),
            "user": self.app_user,
            "updated_at": "2026-10-20T00:00:00Z",
            "issue_url": "https://api.github.test/repos/owner/repo/issues/1",
        }
        for index, size in enumerate(PR342_COMMENT_SIZES[1:], start=1):
            self.parts.append(
                {
                    "id": 600 + index,
                    "body": "x" * size,
                    "user": self.app_user,
                    "updated_at": f"2026-10-20T00:00:{index:02d}Z",
                    "issue_url": "https://api.github.test/repos/owner/repo/issues/1",
                }
            )

    def open(self, request, timeout):
        parsed = urlparse(request.full_url)
        path = parsed.path
        if path.endswith("/issues/1/comments") and request.method == "GET":
            self.calls.append((request.method, request.full_url, None))
            self.part_requests.append(request.method)
            query = parse_qs(parsed.query)
            page = int(query.get("page", [1])[0])
            per_page = int(query.get("per_page", [30])[0])
            since = query.get("since", [None])[0]
            comments = (
                [self.session_comment] if self.session_comment else []
            ) + self.parts
            if since is not None:
                comments = [
                    item for item in comments if item.get("updated_at", "9999") >= since
                ]
            return json_response(comments[(page - 1) * per_page : page * per_page])
        if path.endswith("/issues/1/comments") and request.method == "POST":
            response = super().open(request, timeout)
            self.parts[-1]["updated_at"] = "2026-10-20T00:01:00Z"
            return response
        if (
            self.session_comment is not None
            and path.endswith("/issues/comments/600")
            and request.method == "PATCH"
        ):
            self.session_comment["updated_at"] = "2026-10-20T00:01:00Z"
        return super().open(request, timeout)


class DefaultPublicationCliJourneyTests(unittest.TestCase):
    def setup_files(self, root):
        ledger = LocalSessionLedger(root / "ledger")
        result, key = checkpoint(ledger, count=77)
        documents = {
            "result.json": result.to_dict(),
            "configuration.json": CONFIGURATION,
            "evidence.json": EVIDENCE,
            "admission.json": admission_context_document(None, key),
        }
        for name, document in documents.items():
            (root / name).write_text(json.dumps(document), encoding="utf-8")
        (root / "review.patch").write_text(
            "diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n"
            + DIFF,
            encoding="utf-8",
        )
        self.configuration(root, "legacy")
        return result, ledger.load(IDENTITY).record

    def configuration(self, root, inventory):
        (root / "trusted.yml").write_text(
            "schema: 1\ngithub:\n  operation_entry: enabled\n"
            f"  publication_inventory: {inventory}\n",
            encoding="utf-8",
        )

    def invoke(
        self,
        root,
        state,
        *,
        recover=False,
        actions=False,
        github_session_ledger=False,
    ):
        arguments = [
            "publish",
            "--allow-write",
            "--enable-review",
            "--no-auto-approve",
            "--oidc-token",
            "synthetic",
            "--repository",
            IDENTITY.repository,
            "--repository-id",
            "1",
            "--pull-request",
            "1",
            "--head-sha",
            HEAD,
            "--base-sha",
            BASE,
            "--base-branch",
            "main",
            "--app-slug",
            APP,
            "--diff",
            str(root / "review.patch"),
            *(
                ["--github-session-ledger"]
                if github_session_ledger
                else ["--session-ledger", str(root / "ledger")]
            ),
            "--review-mode",
            "merge-focused",
            "--configuration-context",
            str(root / "configuration.json"),
            "--evidence-context",
            str(root / "evidence.json"),
            "--admission-context",
            str(root / "admission.json"),
            "--config",
            str(root / "trusted.yml"),
            "--outcome",
            str(root / "outcome.json"),
        ]
        arguments += (
            ["--recover-retained"]
            if recover
            else ["--result", str(root / "result.json")]
        )
        environment = {"GITHUB_TOKEN": "synthetic-read"}
        if actions:
            environment["GITHUB_RUN_ID"] = "synthetic-publication-run"
        output, error = io.StringIO(), io.StringIO()
        broker_cls = _HostedBroker if github_session_ledger else _Broker
        with (
            patch.dict(os.environ, environment, clear=True),
            patch.multiple(
                github, GitHubHttp=lambda: state.http, BrokerClient=broker_cls
            ),
            patch.object(
                engine_cli,
                "default_registry",
                side_effect=AssertionError("publication cannot construct inference"),
            ) as registry,
            patch.object(
                ReviewService,
                "run",
                side_effect=AssertionError("publication cannot rerun analysis"),
            ) as analysis,
            redirect_stdout(output),
            redirect_stderr(error),
        ):
            code = github_main(arguments)
        self.assertEqual(registry.call_count, 0)
        self.assertEqual(analysis.call_count, 0)
        return code, output.getvalue(), error.getvalue()

    def assert_complete_publication(self, state, result):
        writes = [
            body
            for method, path, body in state.calls
            if method == "POST" and path.endswith("/pulls/1/reviews")
        ]
        self.assertEqual(len(writes), 1)
        review = writes[0]
        self.assertEqual(review["event"], "COMMENT")
        index = parse_finding_index(
            review["body"], repository_id=1, pull_request=1, head_sha=HEAD
        )
        self.assertEqual(len(index.entries), 77)
        reader_index = ReviewApprovalFinalizer(
            http=state.http
        ).finding_index_from_reviews(
            state.reviews, repository_id=1, pull_request=1, head_sha=HEAD, app_slug=APP
        )
        self.assertEqual(reader_index, index)
        self.assertEqual(
            {item.instance: item.lifecycle for item in index.entries},
            {
                finding_instance_fingerprint(comment): finding_lifecycle_for_comment(
                    comment
                ).fingerprint
                for comment in result.comments
            },
        )
        complete_text = review["body"] + "\n".join(
            item["body"] for item in review.get("comments", [])
        )
        for comment in result.comments:
            self.assertIn(comment.body, complete_text)
        self.assertNotIn("omitted for size", complete_text)
        self.assertFalse(
            any(item.blocking or item.needs_human for item in index.entries)
        )

    def test_trusted_complete_config_publishes_all_77_through_actual_cli_and_reader(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root, state = Path(directory), JourneyState()
            result, before = self.setup_files(root)
            self.configuration(root, "complete")
            code, output, error = self.invoke(root, state)
            self.assertEqual(code, 0, (output, error))
            self.assertIn("published", output)
            self.assert_complete_publication(state, result)
            after = LocalSessionLedger(root / "ledger").load(IDENTITY).record
            self.assertEqual(
                after.completed_initial_reviews, before.completed_initial_reviews
            )
            self.assertEqual(after.transaction.phase, "publication_succeeded")
            self.assertEqual(
                after.completed_verification_rounds,
                before.completed_verification_rounds,
            )

    def fail_legacy(self, root, state):
        result, before = self.setup_files(root)
        code, output, error = self.invoke(root, state)
        self.assertNotEqual(code, 0, (output, error))
        self.assertIn("finding index", error)
        self.assertTrue(
            any(RESULT_COMMENT_MARKER in item["body"] for item in state.parts)
        )
        failed = LocalSessionLedger(root / "ledger").load(IDENTITY).record
        self.assertEqual(failed.transaction.phase, "publication_failed")
        self.assertEqual(
            failed.completed_initial_reviews, before.completed_initial_reviews
        )
        self.assertFalse(
            any(
                method == "POST" and path.endswith("/reviews")
                for method, path, _ in state.calls
            )
        )
        return result, before

    def test_legacy_failure_recovers_from_actual_parts_and_fresh_cli_without_result_file_or_inference(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root, state = Path(directory), JourneyState()
            result, before = self.fail_legacy(root, state)
            retained_posts = state.part_requests.count("POST")
            (root / "result.json").unlink()
            self.configuration(root, "complete")
            # Each invocation constructs the real application and a new ledger
            # owner from disk; only the synthetic external GitHub API persists.
            code, output, error = self.invoke(root, state, recover=True)
            self.assertEqual(code, 0, (output, error))
            self.assert_complete_publication(state, result)
            self.assertEqual(state.part_requests.count("POST"), retained_posts)
            after = LocalSessionLedger(root / "ledger").load(IDENTITY).record
            self.assertEqual(
                after.completed_initial_reviews, before.completed_initial_reviews
            )
            self.assertEqual(after.transaction.phase, "publication_succeeded")

    def accept_analysis(self, state, result, *, changed=False):
        original = result.to_dict()
        original.pop("transaction", None)
        if changed:
            original["summary"] = "Different accepted analysis."
        payload = json.dumps(original, sort_keys=True, separators=(",", ":")).encode()
        port = GitHubIssueCommentOperationPort(
            state.http,
            token="synthetic-operation",
            repository=IDENTITY.repository,
            pull_request=1,
            app_user_id=99,
        )
        outcome = run_hosted_provider(
            GitHubCommentOperationStore(port, app_user_id=99),
            trigger="full-review",
            repository_id=1,
            pull_request=1,
            attestation_digest="a" * 64,
            run_id="synthetic-analysis-run",
            run_attempt="1",
            app_id=99,
            reservation_id="accepted-analysis",
            delivery_id="accepted-analysis",
            grant="accepted-analysis",
            server_started_ms=1000,
            execute=lambda: payload,
            encode=lambda value: value,
            parts=ResultPartStore(port, app_user_id=99),
            now=lambda: 1000,
        )
        self.assertIsInstance(outcome, HostedProviderResult)

    def test_retained_recovery_obeys_actual_accepted_analysis_guard_under_actions(self):
        for changed in (False, True):
            with (
                self.subTest(changed=changed),
                tempfile.TemporaryDirectory() as directory,
            ):
                root, state = Path(directory), JourneyState()
                result, before = self.fail_legacy(root, state)
                self.accept_analysis(state, result, changed=changed)
                (root / "result.json").unlink()
                self.configuration(root, "complete")
                code, output, error = self.invoke(
                    root, state, recover=True, actions=True
                )
                if changed:
                    self.assertNotEqual(code, 0, (output, error))
                    self.assertIn("does not match the accepted operation", error)
                    self.assertFalse(
                        any(
                            method == "POST" and path.endswith("/reviews")
                            for method, path, _ in state.calls
                        )
                    )
                    after = LocalSessionLedger(root / "ledger").load(IDENTITY).record
                    self.assertEqual(
                        after.completed_initial_reviews,
                        before.completed_initial_reviews,
                    )
                else:
                    self.assertEqual(code, 0, (output, error))
                    self.assert_complete_publication(state, result)

    def test_hosted_session_ledger_pr342_comment_shape_stages_and_publishes_within_budget(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result, before = self.setup_files(root)
            state = PR342JourneyState(before)
            self.accept_analysis(state, result)
            for item in state.parts:
                item.setdefault("updated_at", "2026-10-20T00:00:30Z")
            self.configuration(root, "legacy")
            before_legacy_calls = len(state.calls)
            code, output, error = self.invoke(
                root, state, actions=True, github_session_ledger=True
            )
            legacy_calls = len(state.calls) - before_legacy_calls
            self.assertNotEqual(code, 0, (output, error))
            self.assertNotIn("cannot stage complete inventory", error)
            self.assertIn("finding index", error)
            self.assertLessEqual(legacy_calls, 60)

            (root / "result.json").unlink()
            self.configuration(root, "complete")
            before_recover_calls = len(state.calls)
            code, output, error = self.invoke(
                root,
                state,
                recover=True,
                actions=True,
                github_session_ledger=True,
            )
            recover_calls = len(state.calls) - before_recover_calls
            self.assertEqual(code, 0, (output, error))
            self.assertLessEqual(recover_calls, 60)
            self.assert_complete_publication(state, result)
            remote_after = (
                GitHubIssueCommentSessionLedger(
                    state.http, token="synthetic-read", app_slug=APP
                )
                .load(IDENTITY)
                .record
            )
            self.assertIsNotNone(remote_after)
            self.assertEqual(remote_after.transaction.phase, "publication_succeeded")
