"""Public commands with actual broker signatures and synthetic GitHub effects.

No live service credentials, provider calls or downstream PRs are used.
"""

from __future__ import annotations

import copy
import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from review_sensei_github.broker_client import BrokerClient
from review_sensei_github.cli import main as github_main
from review_sensei_github.session_ledger import (
    GitHubIssueCommentSessionLedger,
    parse_session_comment,
    render_session_comment,
)

from review_sensei.bounded_evidence import EvidenceReadBudget
from review_sensei.models import ReviewTransaction
from review_sensei.session import SessionRecord
from tests.fake_github_http import json_response
from tests.test_command_capacity_recovery import (
    APP,
    BASE,
    HEAD,
    IDENTITY,
    CommandCapacityState,
)
from tests.test_media_cli_activation import WORKFLOW, ProductionMediaBrokerBridge

BOT = {"id": 99, "login": APP, "type": "Bot"}
ALICE = {"id": 123, "login": "alice", "type": "User"}
API = "https://api.github.test/repos/owner/repo"


class HostedDecisionState(CommandCapacityState):
    def __init__(self):
        super().__init__(
            complete=True, padded=False, inline_range=(0, 25), human_count=25
        )
        self.resolved_threads.clear()
        transaction = ReviewTransaction.create(
            repository=IDENTITY.repository,
            pull_request=1,
            base_sha=BASE,
            head_sha=HEAD,
            policy_digest="c" * 64,
            configuration_digest="e" * 64,
            evidence_digest="a" * 64,
            reservation_id="f" * 64,
            generation=1,
        ).with_result(self.result.content_digest())
        record = SessionRecord.create(
            IDENTITY,
            now=datetime.now(timezone.utc),
            generation=1,
            transaction=transaction,
            completed_initial_reviews=1,
            last_committed_reservation_id=transaction.reservation_id,
        )
        self.issue_comments = {
            600: {
                "id": 600,
                "user": BOT,
                "issue_url": API + "/issues/1",
                "updated_at": "2026-10-23T13:00:00Z",
                "body": render_session_comment(
                    repository_id=1, pull_request=1, record=record
                ),
            }
        }
        self.source_id = 20
        self.next_id = 601
        self.lose_part_response = False
        self.lose_root_response = False
        self.concurrent_root_change = False

    def source(self, body, identifier):
        self.source_id = identifier
        self.issue_comments[identifier] = {
            "id": identifier,
            "user": ALICE,
            "author_association": "OWNER",
            "issue_url": API + "/issues/1",
            "body": body,
            "updated_at": "2026-10-23T13:00:00Z",
        }

    def broker_authority(self):
        return {
            "pull_request": 1,
            "base": self.base,
            "head": self.head,
            "draft": False,
            "fork": False,
            "source": copy.deepcopy(self.issue_comments.get(self.source_id)),
            "comments": copy.deepcopy(list(self.issue_comments.values())),
            "reviews": [
                {
                    key: copy.deepcopy(value[key])
                    for key in ("id", "user", "commit_id", "state")
                }
                for value in self.reviews
            ],
        }

    def open(self, request, timeout):
        url = urlsplit(request.full_url)
        path, method = url.path, request.method
        issue_list = path.endswith("/issues/1/comments")
        comment = "/issues/comments/" in path
        permission = "/collaborators/" in path
        if not (issue_list or comment or permission):
            return super().open(request, timeout)
        data = json.loads(request.data) if request.data else None
        self.calls.append((method, path, copy.deepcopy(data)))
        if permission:
            return json_response({"permission": "admin", "role_name": "admin"})
        if issue_list:
            if method == "GET":
                query = parse_qs(url.query)
                page = int(query.get("page", ["1"])[0])
                size = int(query.get("per_page", ["100"])[0])
                records = list(self.issue_comments.values())
                return json_response(
                    copy.deepcopy(records[(page - 1) * size : page * size])
                )
            assert method == "POST"
            identifier = self.next_id
            self.next_id += 1
            self.issue_comments[identifier] = {
                "id": identifier,
                "user": BOT,
                "issue_url": API + "/issues/1",
                "updated_at": "2026-10-23T13:00:00Z",
                **data,
            }
            if data["body"].startswith("ReviewSensei immutable evidence part v1"):
                if self.concurrent_root_change:
                    self.concurrent_root_change = False
                    current = parse_session_comment(
                        self.issue_comments[600]["body"],
                        identity=IDENTITY,
                    )
                    self.issue_comments[600]["body"] = render_session_comment(
                        repository_id=1,
                        pull_request=1,
                        record=current.evolve(generation=current.generation + 1),
                    )
                if self.lose_part_response:
                    self.lose_part_response = False
                    raise URLError("synthetic committed part response lost")
            return json_response(copy.deepcopy(self.issue_comments[identifier]), 201)
        identifier = int(path.rsplit("/", 1)[1])
        if method == "PATCH":
            self.issue_comments[identifier].update(data)
            if identifier == 600 and self.lose_root_response:
                self.lose_root_response = False
                raise URLError("synthetic committed root response lost")
        return json_response(copy.deepcopy(self.issue_comments[identifier]))


class ProductionDecisionBrokerBridge(ProductionMediaBrokerBridge):
    def open(self, request, timeout):
        assert 0 < timeout <= 60
        payload = json.loads(request.data)
        path = urlsplit(request.full_url).path
        self.requests.append(payload.get("capability", "verify_session"))
        self.admitted.append(payload["control_budget"]["max_dispatches"] + 1)
        self.attempts.append(
            {"origin": "caller-broker", "method": request.method, "path": path}
        )
        reply = self.call(
            "verify" if path.endswith("/session-grant") else "exchange",
            body=payload,
            authority=self.owner.state.broker_authority(),
        )
        if reply["status"] != 200:
            raise HTTPError(
                request.full_url,
                reply["status"],
                "synthetic external refusal",
                {},
                io.BytesIO(json.dumps(reply["result"]).encode()),
            )
        self.receipts.append(reply["result"]["control_receipt"])
        return json_response(reply["result"])


@unittest.skipUnless(
    os.environ.get("REVIEWSENSEI_MEDIA_BROKER_QUALIFICATION") == "1",
    "explicit production broker qualification required",
)
class HostedDecisionSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.state = HostedDecisionState()
        self.broker = ProductionDecisionBrokerBridge(self)
        self.addCleanup(self.broker.close)
        self.budgets = []

    def command(self, body, identifier):
        import review_sensei_github as github

        self.state.source(body, identifier)

        def factory(**kwargs):
            self.budgets.append(kwargs["control"].budget)
            return BrokerClient(
                broker_url="https://broker.github.test/api/github/token",
                opener=self.broker.open,
                **kwargs,
            )

        attestation = {
            "version": 1,
            "repository": "owner/repo",
            "repository_id": 1,
            "pull_request": 1,
            "head_sha": HEAD,
            "operation": "command",
            "source_comment_id": identifier,
            "run_id": "123",
            "issued_at": int(time.time()),
            "concurrency_group": "reviewsensei-session-1-1",
            "job_workflow_ref": WORKFLOW,
            "job_workflow_sha": "a" * 40,
        }
        output = io.StringIO()
        before_http, before_broker = len(self.state.calls), len(self.broker.attempts)
        with tempfile.TemporaryDirectory() as directory:
            request = Path(directory) / "attestation.json"
            request.write_text(json.dumps(attestation))
            config = Path(directory) / "config.yml"
            config.write_text("schema: 1\ngithub:\n  reviews: auto-approve\n")
            with (
                patch.dict(
                    os.environ,
                    {
                        "GITHUB_TOKEN": "synthetic-read",
                        "GITHUB_RUN_ID": "123",
                        "GITHUB_RUN_ATTEMPT": "1",
                    },
                    clear=True,
                ),
                patch.object(github, "GitHubHttp", return_value=self.state.http),
                patch.object(github, "BrokerClient", side_effect=factory),
                redirect_stdout(output),
                redirect_stderr(output),
            ):
                status = github_main(
                    [
                        "command",
                        "--comment-body",
                        body,
                        "--actor",
                        "alice",
                        "--association",
                        "OWNER",
                        "--repository",
                        "owner/repo",
                        "--repository-id",
                        "1",
                        "--pull-request",
                        "1",
                        "--head-sha",
                        HEAD,
                        "--source-comment-id",
                        str(identifier),
                        "--session-attestation",
                        str(request),
                        "--oidc-token",
                        self.broker.oidc(),
                        "--app-slug",
                        APP,
                        "--config",
                        str(config),
                        "--github-session-ledger",
                        "--allow-write",
                    ]
                )
        self.last_output = output.getvalue()
        self.last_physical = (
            len(self.state.calls)
            - before_http
            + len(self.broker.attempts)
            - before_broker
        )
        self.assertTrue(self.budgets, self.last_output)
        self.assertLessEqual(self.budgets[-1].calls, 64)
        self.assertLessEqual(self.last_physical, self.budgets[-1].calls)
        return status

    def load(self):
        ledger = GitHubIssueCommentSessionLedger(
            self.state.http,
            token="synthetic-read",
            app_slug=APP,
            evidence_budget=EvidenceReadBudget(),
        )
        from review_sensei.disposition import session_dispositions

        result = ledger.load(IDENTITY)
        self.assertEqual(result.status, "ok", str(result) + self.last_output)
        return result.record, session_dispositions(result.record, ledger=ledger)

    def override(self, start, identifier):
        labels = [
            self.state.labels[entry.instance]
            for entry in self.state.entries[start : start + 10]
            if entry.needs_human
        ]
        return self.command(
            "@reviewsensei override " + " ".join(labels) + " acceptable risk",
            identifier,
        )

    def test_public_verify_reports_graphql_failure_without_claiming_approval(self):
        from review_sensei_github.http import GitHubHttp

        for start, event in ((0, 20), (10, 21), (20, 22)):
            self.override(start, event)
        self.assertEqual(len(self.load()[1]), 25)

        def opener(request, timeout):
            path = urlsplit(request.full_url).path
            if path == "/graphql":
                data = json.loads(request.data)
                self.state.calls.append((request.method, path, data))
                return json_response(
                    {"errors": [{"type": "FORBIDDEN", "message": "SECRET-server"}]}
                )
            return self.state.open(request, timeout)

        self.state.http = GitHubHttp(api_url="https://api.github.test", opener=opener)
        self.command("@reviewsensei verify", 23)
        self.assertEqual(len(self.load()[1]), 25)
        self.assertEqual(self.state.checks[-1]["conclusion"], "success")
        self.assertFalse(any(r["state"] == "APPROVED" for r in self.state.reviews))
        lines = self.last_output.splitlines()
        report = json.loads(
            next(
                line.split(": ", 1)[1]
                for line in lines
                if line.startswith("approval-operation-failure: ")
            )
        )
        self.assertEqual(report["cleanup"]["first"]["reason"], "graphql-rejected")
        self.assertEqual(report["approval"]["first"]["reason"], "graphql-rejected")
        self.assertFalse(report["approval_request_attempted"])
        self.assertNotIn("SECRET", self.last_output)

    def test_lost_part_response_does_not_activate_or_invent_decisions(self):
        self.override(0, 20)
        self.override(10, 21)
        self.state.lose_part_response = True
        self.assertEqual(self.override(20, 22), 1)
        record, decisions = self.load()
        self.assertEqual(len(decisions), 20)
        self.assertIsNone(record.disposition_snapshot)
        self.assertTrue(
            any(
                comment["body"].startswith("ReviewSensei immutable evidence part v1")
                for comment in self.state.issue_comments.values()
            )
        )
        self.override(20, 22)
        self.assertEqual(len(self.load()[1]), 25, self.last_output)

    def test_lost_root_response_reloads_committed_decisions_without_duplicate(self):
        self.override(0, 20)
        self.override(10, 21)
        self.state.lose_root_response = True
        self.override(20, 22)
        record, decisions = self.load()
        self.assertEqual(len(decisions), 25, self.last_output)
        generation = record.generation
        self.override(20, 22)
        record, replayed = self.load()
        self.assertEqual(record.generation, generation)
        self.assertEqual(decisions, replayed)
        self.assertEqual(
            [review for review in self.state.reviews if review["state"] == "APPROVED"],
            [],
        )

    def test_concurrent_root_change_leaves_orphan_parts_without_decision_authority(
        self,
    ):
        self.override(0, 20)
        self.override(10, 21)
        self.state.concurrent_root_change = True
        self.assertEqual(self.override(20, 22), 1)
        record, decisions = self.load()
        self.assertEqual(record.generation, 4)
        self.assertEqual(len(decisions), 20)
        self.assertIsNone(record.disposition_snapshot)

    def test_twenty_five_audited_decisions_reach_one_native_approval(self):
        for batch, start in enumerate(range(0, 25, 10)):
            labels = [
                self.state.labels[entry.instance]
                for entry in self.state.entries[start : start + 10]
                if entry.needs_human
            ]
            body = "@reviewsensei override " + " ".join(labels) + " acceptable risk"
            self.command(body, 20 + batch)
            record, decisions = self.load()
            self.assertEqual(len(decisions), min(start + 10, 25), self.last_output)
            self.assertTrue(
                all(
                    decision.actor_role == "admin" and decision.provenance
                    for decision in decisions
                )
            )
        self.assertIsNotNone(record.disposition_snapshot)
        for attempt in range(27):
            self.command("@reviewsensei verify", 30 + attempt)
            approved = [
                review for review in self.state.reviews if review["state"] == "APPROVED"
            ]
            if (
                approved
                and self.state.checks
                and self.state.checks[-1].get("conclusion") == "success"
            ):
                break
        self.assertEqual(len(approved), 1, self.last_output)
        self.assertEqual(approved[0]["commit_id"], HEAD)
        self.assertEqual(len(self.state.resolved_threads), 25)
        self.assertEqual(self.state.checks[-1]["conclusion"], "success")
        before = len(self.broker.requests)
        self.command("@reviewsensei verify", 80)
        self.assertNotIn("thread_resolve", self.broker.requests[before:])
        self.assertEqual(
            len(
                [
                    review
                    for review in self.state.reviews
                    if review["state"] == "APPROVED"
                ]
            ),
            1,
        )

    def test_unfunded_check_after_cleanup_retains_progress_and_defers_new_approval(
        self,
    ):
        for start, event in ((0, 20), (10, 21), (20, 22)):
            self.override(start, event)
        # Synthetic external starting state: retained decisions, reopened
        # eligible threads and no prior native approval. Never a live mutation.
        self.state.resolved_threads.clear()
        self.state.reviews[:] = [
            review for review in self.state.reviews if review["state"] != "APPROVED"
        ]
        self.command("@reviewsensei verify", 30)
        self.assertGreater(len(self.state.resolved_threads), 0, self.last_output)
        self.assertLess(len(self.state.resolved_threads), 25, self.last_output)
        self.assertEqual(len(self.load()[1]), 25)
        self.assertFalse(
            any(review["state"] == "APPROVED" for review in self.state.reviews)
        )
        progress = json.loads(
            next(
                line.split(": ", 1)[1]
                for line in self.last_output.splitlines()
                if line.startswith("approval-progress: ")
            )
        )
        self.assertNotEqual(progress["check_state"], "confirmed")
        self.assertEqual(progress["approval_state"], "not-observed")
        self.assertEqual(progress["next_action"], "retry_verify")

    def test_refused_deferred_check_preserves_existing_approval_without_new_write(self):
        for start, event in ((0, 20), (10, 21), (20, 22)):
            self.override(start, event)
        for attempt in range(27):
            self.command("@reviewsensei verify", 30 + attempt)
            if any(r["state"] == "APPROVED" for r in self.state.reviews):
                break
        self.assertEqual(len(self.state.resolved_threads), 25)
        approved = [
            review for review in self.state.reviews if review["state"] == "APPROVED"
        ]
        self.assertEqual(len(approved), 1)
        original = self.broker.open

        def refused(request, timeout):
            if json.loads(request.data).get("capability") == "check_publish":
                raise HTTPError(
                    request.full_url, 403, "synthetic refusal", {}, io.BytesIO(b"{}")
                )
            return original(request, timeout)

        self.broker.open = refused
        self.command("@reviewsensei verify", 80)
        self.assertEqual(
            [review for review in self.state.reviews if review["state"] == "APPROVED"],
            approved,
        )
        progress = json.loads(
            next(
                line.split(": ", 1)[1]
                for line in self.last_output.splitlines()
                if line.startswith("approval-progress: ")
            )
        )
        self.assertEqual(progress["approval_state"], "observed-existing")
        self.assertNotEqual(progress["check_state"], "confirmed")
        self.assertEqual(progress["next_action"], "reconcile_current_check")
        report = json.loads(
            next(
                line.split(": ", 1)[1]
                for line in self.last_output.splitlines()
                if line.startswith("approval-operation-failure: ")
            )
        )
        self.assertEqual(report["check"]["first"]["stage"], "check.capability")
