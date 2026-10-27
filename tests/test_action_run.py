"""Current command CLI lifecycle with actual adapters and synthetic external HTTP.

The broker in this fixture checks/returns current v3 scope but does not sign real
credentials. Production Worker issuance is separately qualified; no network or
provider inference is needed for commands.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import test_review_run as review_run_fixtures
from fake_github_http import json_response
from review_sensei_github import action_run, review_run
from review_sensei_github import cli as package_cli
from review_sensei_github.api_run_requests import ApiRunRequestTransport
from review_sensei_github.broker_client import BrokerClient
from review_sensei_github.http import GitHubHttp
from review_sensei_github.run_authority import REQUEST_KEYS, parse_authority
from review_sensei_github.session_ledger import parse_session_comment
from test_api_work_persistence import HEAD, IDENTITY
from test_review_run import (
    EntryFixture,
    _CompletionFixture,
    fixture_process_environment,
)

from review_sensei.api_work import ApiExecutionOwner, ApiRunAccounting
from review_sensei.bounded_evidence import EvidenceReadBudget
from review_sensei.errors import ReviewInputError


class CommandEntryFixture(EntryFixture):
    SOURCE_ID = 41
    SOURCE_STAMP = "2026-10-26T00:00:00Z"

    def __init__(self, directory, *, limit=1024):
        super().__init__(directory, limit=limit)
        self.command_active = False
        self.command_body = "@reviewsensei review pause"
        self.source = None
        self.source_reads = 0
        self.before_source_read = None
        self.before_business_write = None
        self.grant_fault = None
        self.action_fault = None
        self.business_writes = 0
        self.closure_writes = 0
        self.broker_requests = []
        self.review_updates = 0
        self.review_put_attempts = 0
        self.put_fault = None
        self.resolved_threads = set()
        self.past_jobs = {}
        self.stdout = self.error = ""
        self.exception = None

    def _meter(self, request):
        assert (
            len(self.accounted.observations)
            == sum(a == self.attempt for a, _, _ in self.trace) + 1
        )
        self.trace.append(
            (self.attempt, request.get_method(), urlsplit(request.full_url).path)
        )

    def _source_scope(self):
        return {
            "kind": "issue_comment",
            "id": self.SOURCE_ID,
            "updated_at": self.SOURCE_STAMP,
            "body_sha256": hashlib.sha256(self.command_body.encode()).hexdigest(),
        }

    def _expected_authority(self):
        return {
            "version": 3,
            "purpose": "command",
            "repository": "owner/repo",
            "repository_id": 123,
            "pull_request": 7,
            "head_sha": self.requested_head,
            "run_id": str(self.run_id),
            "run_attempt": self.attempt,
            "job": "command",
            "job_workflow_ref": "malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@refs/tags/v5",
            "job_workflow_sha": "a" * 40,
            "audience": "reviewsensei-run-action",
            "source": self._source_scope(),
        }

    def _actor(self):
        return {"login": "alice", "type": "User", "association": "OWNER"}

    def _broker_response(self, request):
        self._meter(request)
        payload = json.loads(request.data)
        self.broker_requests.append(copy.deepcopy(payload))
        verifying = urlsplit(request.full_url).path.endswith("/session-grant")
        authority = parse_authority(payload["session_attestation"], grant=verifying)
        expected = self._expected_authority()
        assert set(authority) == REQUEST_KEYS | ({"actor"} if verifying else set())
        assert all(authority[key] == value for key, value in expected.items())
        receipt = {
            **payload["control_budget"],
            "dispatched": min(self.fanout, payload["control_budget"]["max_dispatches"]),
        }
        if verifying:
            assert authority["actor"] == self._actor()
            return json_response(
                {"session_attestation": authority, "control_receipt": receipt}
            )
        value = {
            "token": "synthetic-" + payload["capability"],
            "capability": payload["capability"],
            "control_receipt": receipt,
        }
        if payload["capability"] == "review_session":
            returned = {**authority, "actor": self._actor()}
            if self.grant_fault == "purpose":
                returned.update(purpose="reply", job="hosted")
            elif self.grant_fault == "run":
                returned["run_id"] = str(self.run_id + 1)
            elif self.grant_fault == "head":
                returned["head_sha"] = "f" * 40
            elif self.grant_fault == "source":
                returned["source"] = {**returned["source"], "id": self.SOURCE_ID + 1}
            elif self.grant_fault == "obsolete":
                returned["version"] = 1
            value.update(
                app_id=999,
                session_state="known" if self.comment else "enrolled",
                reservation_owner={
                    "run_id": str(self.run_id),
                    "head_sha": self.requested_head,
                },
                session_grant="sg1.9999999999999." + "A" * 43,
                session_attestation=returned,
            )
        return json_response(value)

    def external(self, request, timeout):
        if not self.command_active:
            return super().external(request, timeout)
        path, method = urlsplit(request.full_url).path, request.get_method()
        if urlsplit(request.full_url).hostname == "broker.github.test":
            return self._broker_response(request)
        if (
            path == f"/repos/owner/repo/issues/comments/{self.SOURCE_ID}"
            and method == "GET"
        ):
            self._meter(request)
            self.source_reads += 1
            if self.before_source_read:
                self.before_source_read(self)
            return json_response(copy.deepcopy(self.source))
        if (
            path == "/repos/owner/repo/collaborators/alice/permission"
            and method == "GET"
        ):
            self._meter(request)
            return json_response({"permission": "admin"})
        if path.startswith("/repos/owner/repo/pulls/comments/") and method == "GET":
            self._meter(request)
            identifier = int(path.rsplit("/", 1)[1])
            return json_response(
                copy.deepcopy(
                    next(item for item in self.inline if item["id"] == identifier)
                )
            )
        if path == "/graphql" and method == "POST":
            self._meter(request)
            payload = json.loads(request.data)
            if payload.get("operationName") == "ResolveReviewThread":
                identifier = payload["variables"]["input"]["threadId"]
                self.resolved_threads.add(identifier)
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
                identifier = payload["variables"]["id"]
                return json_response(
                    {
                        "data": {
                            "node": {
                                "id": identifier,
                                "isResolved": identifier in self.resolved_threads,
                            }
                        }
                    }
                )
            return json_response(
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "reviewThreads": {
                                    "nodes": [
                                        {
                                            "id": "PRRT_" + str(item["id"]),
                                            "isResolved": "PRRT_" + str(item["id"])
                                            in self.resolved_threads,
                                            "comments": {
                                                "nodes": [
                                                    {
                                                        "databaseId": item["id"],
                                                        "body": item["body"],
                                                        "author": {
                                                            "login": "reviewsensei[bot]",
                                                            "__typename": "Bot",
                                                        },
                                                    }
                                                ]
                                            },
                                        }
                                        for item in self.inline
                                    ],
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
        if path.startswith("/repos/owner/repo/pulls/7/reviews/") and method == "PUT":
            self._meter(request)
            self.review_put_attempts += 1
            identifier = int(path.rsplit("/", 1)[1])
            selected = next(
                review for review in self.reviews if review["id"] == identifier
            )
            fault, self.put_fault = self.put_fault, None
            if fault == "absent":
                raise TimeoutError("synthetic lost review update response")
            selected["body"] = json.loads(request.data)["body"]
            self.review_updates += 1
            if fault == "stored":
                raise TimeoutError("synthetic lost review update response")
            return json_response(copy.deepcopy(selected))
        if method == "PATCH" and path == "/repos/owner/repo/issues/comments/1000000":
            candidate = parse_session_comment(
                json.loads(request.data)["body"], identity=IDENTITY
            )
            previous = parse_session_comment(self.comment["body"], identity=IDENTITY)
            business = (
                candidate.operator_paused != previous.operator_paused
                or candidate.dispositions != previous.dispositions
                or candidate.historical_assessments != previous.historical_assessments
            )
            closing = (
                candidate.api_run_accounting["action"] is not None
                and candidate.api_run_accounting["action"]["phase"] == "terminal"
            )
            if closing:
                self.closure_writes += 1
            if business:
                self.business_writes += 1
                if self.before_business_write:
                    self.before_business_write(self)
            fault = self.action_fault
            if fault and (
                business
                and fault.startswith("business")
                or closing
                and fault.startswith("closure")
            ):
                self.action_fault = None
                if fault.endswith("stored"):
                    super().external(request, timeout)
                else:
                    self._meter(request)
                raise TimeoutError("synthetic lost action write response")
        response = super().external(request, timeout)
        if "/actions/runs/" in path and path.endswith("/jobs"):
            run_id = int(path.split("/actions/runs/")[1].split("/")[0])
            if run_id in self.past_jobs:
                value = json.loads(response.read(524288))
                value["jobs"][0]["name"] = "ReviewSensei / " + self.past_jobs[run_id]
                return json_response(value)
        return response

    def select_command(self, body, *, new_run=False):
        if new_run:
            self.past_jobs[self.run_id] = self.owner_job
            self.run_id += 1
            self.trace.clear()
            self.transports.clear()
            self.worker_observations.clear()
        self.command_active = True
        self.owner_job = "command"
        self.event_name = "issue_comment"
        self.command_body = body
        self.source = {
            "id": self.SOURCE_ID,
            "body": body,
            "updated_at": self.SOURCE_STAMP,
            "user": {"login": "alice", "type": "User", "id": 42},
            "author_association": "OWNER",
            "issue_url": "https://api.github.test/repos/owner/repo/issues/7",
        }

    def invoke_command(self, body=None, *, extra=(), event_override=None):
        if body is not None or not self.command_active:
            self.select_command(body or self.command_body)
        event = {
            "issue": {"number": 7, "pull_request": {}},
            "comment": copy.deepcopy(self.source),
        }
        if event_override is not None:
            event = event_override
        event_file = self.root / "command-event.json"
        event_file.write_text(json.dumps(event))
        fixture = self
        original_bootstrap = action_run.bootstrap

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

        def capture_run(*args, **kwargs):
            run = original_bootstrap(*args, **kwargs)
            fixture.selected = run
            after_bootstrap = getattr(fixture, "after_action_bootstrap", None)
            if after_bootstrap is not None:
                after_bootstrap(run)
            return run

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
            "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "synthetic",
            "ACTIONS_ID_TOKEN_REQUEST_URL": "https://oidc.github.test/token?fixture=1",
            # The runner hint is deliberately unusable. App identity comes
            # from the authenticated current broker session response.
            "REVIEWSENSEI_APP_ID": "0",
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
                    patch.object(action_run, "bootstrap", side_effect=capture_run),
                    patch.object(
                        EvidenceReadBudget,
                        "__init__",
                        side_effect=AssertionError("no replacement phase budget"),
                    ),
                    redirect_stdout(output),
                    redirect_stderr(errors),
                ):
                    stack.enter_context(scope)
                return package_cli.main(
                    [
                        "command-run",
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
                        self.requested_head,
                        "--source-comment-id",
                        str(self.SOURCE_ID),
                        "--comment-body",
                        self.command_body,
                        "--actor",
                        "untrusted-hint",
                        "--association",
                        "NONE",
                        "--config",
                        str(self.root / ".reviewsensei.yml"),
                        "--github-session-ledger",
                        "--allow-write",
                        *extra,
                    ]
                )
        except Exception as error:
            import traceback

            self.exception = error
            errors.write(traceback.format_exc())
            return 2
        finally:
            os.chdir(previous_directory)
            self.stdout, self.error = output.getvalue(), errors.getvalue()


class HistoricalCommandFixture(CommandEntryFixture, _CompletionFixture):
    """Combine external command HTTP with actual historical review metadata."""

    def __init__(self, directory, mode="refused", *, comments=None):
        super().__init__(directory, limit=512)
        self.mode = mode
        self.provider_comments = comments


def historical_command_case(directory, *, comments=None, complete=False):
    f = review_run_fixtures.ConsecutiveReviewRunTests.historical_case(
        directory,
        lambda path, mode: HistoricalCommandFixture(path, mode, comments=comments),
    )
    if complete:
        previous = parse_session_comment(f.comment["body"], identity=IDENTITY)
        f.requested_base = f.live_base = previous.transaction.base_sha
    f.provider_comments = None if complete else []
    if f.invoke(human=True) != 0:
        raise AssertionError(f.error)
    return f


def historical_body(hold, *, instance=None, action="dismiss"):
    instance = instance or hold["assessments"][0]["instance"]
    return (
        "@reviewsensei "
        + action
        + " "
        + instance
        + ' --reason "historical:'
        + hold["scope_sha256"]
        + ' The earlier assessment has an explicit evidence-backed owner decision."'
    )


class CurrentCommandEntryTests(unittest.TestCase):
    def test_historical_assessment_decision_is_distinct_and_cannot_clear_incomplete_coverage(
        self,
    ):
        from review_sensei_github.publication import approval_eligibility_from_body

        for action in ("dismiss", "accept-risk"):
            with (
                self.subTest(action=action),
                tempfile.TemporaryDirectory() as directory,
            ):
                f = historical_command_case(directory)
                old = parse_session_comment(f.comment["body"], identity=IDENTITY)
                hold = copy.deepcopy(old.historical_assessments)
                self.assertTrue(hold["assessments"])
                self.assertIn("Historical assessments", f.reviews[-1]["body"])
                self.assertEqual(f.check_runs[-1]["conclusion"], "action_required")
                before_provider = f.provider_calls
                body = historical_body(hold, action=action)
                f.select_command(body, new_run=True)
                self.assertEqual(f.invoke_command(), 0, f.error + f.stdout)
                current = parse_session_comment(f.comment["body"], identity=IDENTITY)
                state = current.historical_assessments
                self.assertEqual(state["source"], hold["source"])
                self.assertEqual(len(state["settlements"]), 1)
                decision = state["settlements"][0]
                self.assertEqual(decision["action"], action)
                self.assertEqual(decision["head_sha"], f.requested_head)
                self.assertTrue(
                    decision["reason"].startswith("historical:" + hold["scope_sha256"])
                )
                self.assertEqual(
                    decision["source_body_sha256"],
                    hashlib.sha256(body.encode()).hexdigest(),
                )
                self.assertEqual(current.dispositions, old.dispositions)
                eligibility = approval_eligibility_from_body(f.reviews[-1]["body"])
                self.assertEqual(eligibility.historical_assessments["pending"], [])
                self.assertEqual(eligibility.facts.review_status, "partial")
                self.assertEqual(f.check_runs[-1]["conclusion"], "action_required")
                self.assertFalse(any(r["state"] == "APPROVED" for r in f.reviews))
                self.assertEqual(
                    current.api_run_accounting["action"]["phase"], "terminal"
                )
                self.assertEqual(f.provider_calls, before_provider)
                self.assertEqual(f.accounted.accounting.configuration.limit, 512)
                # A later ordinary verify retains the source independently of
                # the newest accounting archive, without another decision.
                f.SOURCE_ID += 1
                f.select_command("@reviewsensei verify", new_run=True)
                self.assertEqual(f.invoke_command(), 0, f.error + f.stdout)
                verified = parse_session_comment(f.comment["body"], identity=IDENTITY)
                self.assertEqual(verified.historical_assessments, state)
                self.assertEqual(f.check_runs[-1]["conclusion"], "action_required")
                self.assertEqual(f.provider_calls, before_provider)

    def test_historical_complete_empty_rescan_and_partial_decisions_keep_all_obligations(
        self,
    ):
        from review_sensei_github.publication import approval_eligibility_from_body
        from test_review_run_rescan import request

        comments = [
            {
                "path": "src/app.py",
                "line": 2,
                "side": "RIGHT",
                "body": "Confirm the external caller contract " + str(i) + ".",
                "severity": "high",
                "blocking": False,
                "needs_human": True,
            }
            for i in range(2)
        ]
        with tempfile.TemporaryDirectory() as directory:
            f = historical_command_case(directory, comments=comments)
            initial = parse_session_comment(f.comment["body"], identity=IDENTITY)
            hold = copy.deepcopy(initial.historical_assessments)
            self.assertEqual(len(hold["assessments"]), 2)
            head = f.requested_head
            request(f, 9, "rescan")
            f.requested_head = f.live_head = head
            self.assertEqual(f.invoke(human=True), 0, f.error)
            f.review_intent = f.event_document = None
            f.event_name = "pull_request"
            result = json.loads((f.root / "review.json").read_text())
            self.assertEqual(result["comments"], [])
            self.assertEqual(result["review_status"], "complete")
            eligibility = approval_eligibility_from_body(f.reviews[-1]["body"])
            self.assertEqual(eligibility.facts.review_status, "complete")
            self.assertTrue(eligibility.facts.has_human_adjudication_findings)
            self.assertFalse(eligibility.historical_assessments["fresh_human_pending"])
            self.assertEqual(len(eligibility.historical_assessments["pending"]), 2)
            self.assertFalse(any(r["state"] == "APPROVED" for r in f.reviews))
            for index, assessment in enumerate(hold["assessments"]):
                f.SOURCE_ID += 1
                body = historical_body(
                    hold,
                    instance=assessment["instance"],
                    action="dismiss" if index == 0 else "accept-risk",
                )
                f.select_command(body, new_run=True)
                before_provider = f.provider_calls
                self.assertEqual(f.invoke_command(), 0, f.error + f.stdout)
                record = parse_session_comment(f.comment["body"], identity=IDENTITY)
                self.assertEqual(
                    record.historical_assessments["source"], hold["source"]
                )
                self.assertEqual(
                    len(record.historical_assessments["settlements"]), index + 1
                )
                # Select the current finding review rather than an approval's
                # intentionally index-free confirmation body.
                current = next(
                    r for r in reversed(f.reviews) if r["state"] == "COMMENTED"
                )
                eligibility = approval_eligibility_from_body(current["body"])
                self.assertEqual(
                    len(eligibility.historical_assessments["pending"]), 1 - index
                )
                self.assertEqual(
                    f.check_runs[-1]["conclusion"],
                    "action_required" if index == 0 else "success",
                )
                self.assertEqual(
                    sum(r["state"] == "APPROVED" for r in f.reviews), index
                )
                self.assertEqual(f.provider_calls, before_provider)
                self.assertEqual(f.accounted.accounting.configuration.limit, 512)
            settled = copy.deepcopy(record.historical_assessments)
            # A future fresh recurrence is independently adjudicated. Prior
            # historical settlements never enter generic finding dispositions.
            f.past_jobs[f.run_id] = f.owner_job
            f.command_active = False
            f.owner_job = "hosted"
            f.event_name = "pull_request"
            f.next_run()
            f.live_head = f.requested_head = "2" * 40
            f.live_base = f.requested_base = "1" * 40
            f.provider_comments = comments
            self.assertEqual(f.invoke(human=True), 0, f.error)
            recurrence = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertEqual(recurrence.historical_assessments, settled)
            self.assertEqual(recurrence.dispositions, ())
            self.assertNotEqual(f.check_runs[-1]["conclusion"], "success")
            self.assertFalse(
                any(
                    r["state"] == "APPROVED" and r["commit_id"] == f.requested_head
                    for r in f.reviews
                )
            )
            f.SOURCE_ID += 1
            f.select_command(historical_body(hold), new_run=True)
            self.assertEqual(f.invoke_command(), 0, f.error + f.stdout)
            self.assertIn("collides with a fresh finding", f.stdout)
            self.assertEqual(
                parse_session_comment(
                    f.comment["body"], identity=IDENTITY
                ).historical_assessments,
                settled,
            )

    def test_historical_assessment_refusal_never_falls_through_or_dismisses_a_fresh_finding(
        self,
    ):
        for fault in ("scope", "malformed", "fresh-collision"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                f = historical_command_case(
                    directory, complete=fault == "fresh-collision"
                )
                before = parse_session_comment(f.comment["body"], identity=IDENTITY)
                hold = copy.deepcopy(before.historical_assessments)
                body = historical_body(hold)
                if fault == "scope":
                    body = body.replace(hold["scope_sha256"], "0" * 64)
                elif fault == "malformed":
                    body = body.replace("historical:", "Historical:")
                before_body = f.comment["body"]
                f.select_command(body, new_run=True)
                code = f.invoke_command()
                self.assertEqual(
                    code, 1 if fault == "malformed" else 0, f.error + f.stdout
                )
                after = parse_session_comment(f.comment["body"], identity=IDENTITY)
                self.assertEqual(after.historical_assessments, hold)
                self.assertEqual(after.dispositions, before.dispositions)
                self.assertFalse(any(r["state"] == "APPROVED" for r in f.reviews))
                self.assertEqual(f.check_runs[-1]["conclusion"], "action_required")
                if fault == "malformed":
                    self.assertEqual(f.comment["body"], before_body)
                    self.assertFalse(f.trace)
                else:
                    self.assertIn("refused", f.stdout.lower())
                    self.assertEqual(
                        after.api_run_accounting["action"]["phase"], "terminal"
                    )

    def test_short_historical_fingerprints_refuse_before_enrollment(self):
        with tempfile.TemporaryDirectory() as directory:
            f = historical_command_case(directory)
            before_body = f.comment["body"]
            hold = parse_session_comment(
                before_body, identity=IDENTITY
            ).historical_assessments
            instance = hold["assessments"][0]["instance"]
            before_provider = f.provider_calls
            for action in ("dismiss", "accept-risk"):
                for length in range(16, 64):
                    with self.subTest(action=action, length=length):
                        f.select_command(
                            historical_body(
                                hold, instance=instance[:length], action=action
                            ),
                            new_run=True,
                        )
                        self.assertEqual(f.invoke_command(), 1, f.error + f.stdout)
                        self.assertIn("full instance fingerprint", f.error)
                        self.assertEqual(f.comment["body"], before_body)
                        self.assertFalse(f.trace)
                        self.assertEqual(f.business_writes, 0)
                        self.assertEqual(f.provider_calls, before_provider)

    def test_historical_assessment_unknown_writes_preserve_pending_state_without_replay(
        self,
    ):
        for fault in ("business-absent", "business-stored", "put-absent", "put-stored"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                f = historical_command_case(directory)
                hold = parse_session_comment(
                    f.comment["body"], identity=IDENTITY
                ).historical_assessments
                body = historical_body(hold)
                f.select_command(body, new_run=True)
                if fault.startswith("business"):
                    f.action_fault = fault
                else:
                    f.put_fault = fault.split("-", 1)[1]
                code = f.invoke_command()
                self.assertEqual(
                    code, 0 if fault == "business-stored" else 1, f.error + f.stdout
                )
                record = parse_session_comment(f.comment["body"], identity=IDENTITY)
                self.assertEqual(
                    len(record.historical_assessments["settlements"]),
                    0 if fault == "business-absent" else 1,
                )
                self.assertFalse(any(r["state"] == "APPROVED" for r in f.reviews))
                self.assertEqual(f.business_writes, 1)
                if fault.startswith("put"):
                    self.assertEqual(f.review_put_attempts, 1)
                if fault != "business-stored":
                    self.assertEqual(
                        record.api_run_accounting["action"]["phase"], "pending"
                    )
                    before_blocks = copy.deepcopy(record.api_run_accounting["blocks"])
                    f.attempt = 2
                    self.assertNotEqual(f.invoke_command(), 0)
                    self.assertEqual(f.business_writes, 1)
                    after = parse_session_comment(f.comment["body"], identity=IDENTITY)
                    self.assertEqual(
                        after.historical_assessments, record.historical_assessments
                    )
                    for block in before_blocks:
                        self.assertIn(block, after.api_run_accounting["blocks"])
                    self.assertEqual(
                        after.api_run_accounting["action"]["phase"], "pending"
                    )

    def test_historical_hold_is_not_removable_and_unverified_v6_cannot_approve(self):
        from review_sensei_github.approval import ReviewApprovalEligibility
        from review_sensei_github.publication import (
            ReviewApprovalFinalizer,
            approval_eligibility_from_body,
        )

        with tempfile.TemporaryDirectory() as directory:
            f = historical_command_case(directory)
            record = parse_session_comment(f.comment["body"], identity=IDENTITY)
            with self.assertRaisesRegex(ReviewInputError, "cannot be removed"):
                record.evolve(historical_assessments=None)
            edited = copy.deepcopy(record.historical_assessments)
            edited["assessments"] = []
            with self.assertRaises(ReviewInputError):
                record.evolve(historical_assessments=edited)
            eligibility = approval_eligibility_from_body(f.reviews[-1]["body"])
            document = eligibility.to_dict()
            self.assertEqual(document["schema_version"], "6")
            for invalid in (
                {**document, "unknown": True},
                {**document, "historical_assessments": None},
                {**document, "current": document},
                {
                    **document,
                    "historical_assessments": {
                        **document["historical_assessments"],
                        "pending": None,
                    },
                },
            ):
                with self.assertRaises(ReviewInputError):
                    ReviewApprovalEligibility.from_dict(invalid)
            forged = copy.deepcopy(document)
            forged["historical_assessments"]["pending"] = []
            forged["current"]["facts"]["review_status"] = "complete"
            restored = ReviewApprovalEligibility.from_dict(forged)

            def forbidden(request, timeout):
                self.fail("unverified historical eligibility reached a transport")

            finalizer = ReviewApprovalFinalizer(http=GitHubHttp(opener=forbidden))
            outcome = finalizer.finalize(
                token="synthetic",
                repository="owner/repo",
                pull_request=7,
                head_sha=f.requested_head,
                app_slug="reviewsensei[bot]",
                eligibility=restored,
            )
            self.assertEqual(outcome.status, "approval_withheld")
            self.assertEqual(outcome.diagnostic, "historical_assessment_unverified")

    def test_pause_and_status_complete_one_metered_action_without_inference(self):
        for body, paused in (
            ("@reviewsensei review pause", True),
            ("@reviewsensei review status", False),
        ):
            with self.subTest(body=body), tempfile.TemporaryDirectory() as directory:
                f = CommandEntryFixture(directory)
                self.assertEqual(f.invoke_command(body), 0, (f.error, f.exception))
                record = parse_session_comment(f.comment["body"], identity=IDENTITY)
                accounting = ApiRunAccounting.from_document(record.api_run_accounting)
                self.assertEqual(record.operator_paused, paused)
                self.assertEqual(accounting.action["phase"], "terminal")
                self.assertEqual(accounting.action["intent"]["purpose"], "command")
                self.assertEqual(f.provider_calls, 0)
                self.assertEqual(f.selected.ledger.authenticated_app_id, 999)
                self.assertEqual(f.business_writes, int(paused))
                self.assertEqual(len(f.accounted.observations), len(f.trace))
                self.assertTrue(f.broker_requests)
                self.assertTrue(
                    all(
                        p["session_attestation"]["version"] == 3
                        for p in f.broker_requests
                    )
                )

    def test_foreign_or_obsolete_grant_never_initializes_a_native_writer(self):
        for fault in ("purpose", "run", "head", "source", "obsolete"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                f = CommandEntryFixture(directory)
                f.grant_fault = fault
                self.assertNotEqual(f.invoke_command(), 0)
                self.assertIsNone(f.comment)
                self.assertEqual(f.business_writes, 0)
                self.assertFalse(f.reviews)
                self.assertFalse(any(method == "PATCH" for _, method, _ in f.trace))

    def test_old_selectors_and_mismatched_event_refuse_before_business_http(self):
        cases = (
            (("--oidc-token", "untrusted-old-selector"), None),
            (("--session-attestation", "old-authority.json"), None),
            (("--session-ledger", "old-local-ledger"), None),
            (
                (),
                {
                    "issue": {"number": 8},
                    "comment": {
                        "id": 41,
                        "body": "@reviewsensei review pause",
                        "updated_at": CommandEntryFixture.SOURCE_STAMP,
                    },
                },
            ),
        )
        for extra, event in cases:
            with (
                self.subTest(extra=extra, event=event),
                tempfile.TemporaryDirectory() as directory,
            ):
                f = CommandEntryFixture(directory)
                self.assertNotEqual(
                    f.invoke_command(extra=extra, event_override=event), 0
                )
                self.assertFalse(f.trace)
                self.assertIsNone(f.comment)

    def test_source_edit_and_owner_takeover_preserve_pending_intent_without_effect(
        self,
    ):
        for fault in ("source", "owner"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                f = CommandEntryFixture(directory)

                def change(current):
                    if current.source_reads != 1:
                        return
                    if fault == "source":
                        current.source["body"] += " edited"
                    else:

                        def takeover(record):
                            state = ApiRunAccounting.from_document(
                                record.api_run_accounting
                            )
                            return record.evolve(
                                generation=record.generation + 1,
                                api_run_accounting=state.claim_startup(
                                    ApiExecutionOwner(2, "command", 1)
                                ).to_document(),
                            )

                        current.rewrite_record(mutate=takeover)

                f.before_source_read = change
                self.assertNotEqual(f.invoke_command(), 0)
                record = parse_session_comment(f.comment["body"], identity=IDENTITY)
                self.assertFalse(record.operator_paused)
                self.assertEqual(f.business_writes, 0)
                self.assertEqual(
                    record.api_run_accounting["action"]["phase"], "pending"
                )
                self.assertFalse(
                    record.api_run_accounting["action"]["execution_started"]
                )
                self.assertFalse(f.reviews)

    def test_overlap_and_stale_head_refuse_before_native_enrollment(self):
        for fault in ("overlap", "head"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                f = CommandEntryFixture(directory)
                if fault == "overlap":
                    f.overlap = True
                else:
                    f.requested_head = "f" * 40
                self.assertNotEqual(f.invoke_command(), 0)
                self.assertIsNone(f.comment)
                self.assertEqual(f.business_writes, 0)

    def test_unfunded_policy_refuses_before_any_http_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            f = CommandEntryFixture(directory, limit=1)
            self.assertNotEqual(f.invoke_command(), 0)
            self.assertFalse(f.trace)
            self.assertEqual(f.business_writes, 0)
            self.assertIsNone(f.comment)

    def test_lost_business_and_closure_responses_never_repeat_a_mutation(self):
        for stage in ("business", "closure"):
            for outcome in ("stored", "absent"):
                with (
                    self.subTest(stage=stage, outcome=outcome),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    f = CommandEntryFixture(directory)
                    f.action_fault = stage + "-" + outcome
                    result = f.invoke_command()
                    record = parse_session_comment(f.comment["body"], identity=IDENTITY)
                    self.assertEqual(f.business_writes, 1)
                    self.assertEqual(
                        record.operator_paused,
                        stage == "closure" or outcome == "stored",
                    )
                    self.assertEqual(
                        f.closure_writes, int(stage == "closure" or outcome == "stored")
                    )
                    self.assertEqual(
                        result == 0, outcome == "stored", (f.error, f.exception)
                    )
                    self.assertEqual(
                        record.api_run_accounting["action"]["phase"],
                        "terminal" if outcome == "stored" else "pending",
                    )
                    self.assertGreaterEqual(f.accounted.accounting.uncertain, 1)
                    self.assertFalse(any(r["state"] == "APPROVED" for r in f.reviews))
                    if outcome == "absent":
                        retained = f.comment["body"]
                        f.select_command("@reviewsensei review status", new_run=True)
                        self.assertNotEqual(f.invoke_command(), 0)
                        self.assertEqual(f.comment["body"], retained)
                        self.assertEqual(f.business_writes, 1)

    def test_informational_worker_fanout_never_changes_caller_charges(self):
        measurements = []
        for fanout in (9, 12, 30):
            with tempfile.TemporaryDirectory() as directory:
                f = CommandEntryFixture(directory)
                f.fanout = fanout
                self.assertEqual(f.invoke_command(), 0, (f.error, f.exception))
                measurements.append(
                    (
                        len(f.trace),
                        f.accounted.accounting.confirmed,
                        f.accounted.accounting.reserved_undispatched,
                        f.accounted.accounting.remaining,
                    )
                )
        self.assertEqual(measurements[0], measurements[1])
        self.assertEqual(measurements[0], measurements[2])

    def test_put_requires_confirmation_tail_before_physical_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            f = CommandEntryFixture(directory)
            self.assertEqual(f.invoke_command(), 0, (f.error, f.exception))
            while f.accounted.usable > 2:
                f.selected.http.request(
                    "GET", "/repos/owner/repo/pulls/7", token=f.selected.ledger.token
                )
            before = len(f.trace)
            confirmed = f.accounted.accounting.confirmed
            with self.assertRaisesRegex(ReviewInputError, "tail is unfunded"):
                f.selected.http.request(
                    "PUT",
                    "/repos/owner/repo/pulls/7/reviews/100",
                    token=f.selected.ledger.token,
                    body={"body": "must not dispatch"},
                )
            self.assertEqual(len(f.trace), before)
            self.assertEqual(f.accounted.accounting.confirmed, confirmed)
            self.assertEqual(f.review_put_attempts, 0)

    def test_uncertain_review_put_keeps_decision_and_blocks_replay_or_approval(self):
        from review_sensei_github.publication import parse_finding_index

        for fault in ("stored", "absent"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                f = CommandEntryFixture(directory)
                self.assertEqual(f.invoke(human=True), 0, f.error)
                index = parse_finding_index(
                    f.reviews[-1]["body"],
                    repository_id=123,
                    pull_request=7,
                    head_sha=HEAD,
                )
                reference = "RS-" + index.entries[0].instance[:6].upper()
                f.select_command(
                    "@reviewsensei override " + reference + " acceptable risk",
                    new_run=True,
                )
                f.put_fault = fault
                self.assertNotEqual(f.invoke_command(), 0)
                current = parse_session_comment(f.comment["body"], identity=IDENTITY)
                self.assertEqual(len(current.dispositions), 1)
                self.assertEqual(
                    current.api_run_accounting["action"]["phase"], "pending"
                )
                self.assertEqual(f.review_put_attempts, 1)
                self.assertEqual(f.review_updates, int(fault == "stored"))
                self.assertGreaterEqual(f.accounted.accounting.uncertain, 1)
                # The physical transport marks this actual PUT as an unresolved
                # mutation even when its response never reaches the adapter.
                self.assertTrue(
                    any(
                        o.get("mutation") and o["outcome"] == "unknown"
                        for o in f.accounted.observations
                    )
                )
                before = f.comment["body"]
                f.SOURCE_ID += 1
                f.select_command("@reviewsensei verify", new_run=True)
                self.assertNotEqual(f.invoke_command(), 0)
                self.assertEqual(f.review_put_attempts, 1)
                self.assertEqual(f.comment["body"], before)
                self.assertFalse(any(r["state"] == "APPROVED" for r in f.reviews))

    def test_inherited_successful_review_cannot_roll_over_an_unfinished_command(self):
        with tempfile.TemporaryDirectory() as directory:
            f = CommandEntryFixture(directory)
            self.assertEqual(f.invoke(human=True), 0, f.error)
            reviewed = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertEqual(reviewed.transaction.phase, "publication_succeeded")
            f.select_command("@reviewsensei review pause", new_run=True)
            f.action_fault = "business-absent"
            self.assertNotEqual(f.invoke_command(), 0)
            pending = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertEqual(
                pending.transaction.transaction_id, reviewed.transaction.transaction_id
            )
            self.assertEqual(pending.transaction.phase, "publication_succeeded")
            self.assertEqual(pending.api_run_accounting["action"]["phase"], "pending")
            saved_root, saved_parts = f.comment["body"], copy.deepcopy(f.extra_comments)
            f.select_command("@reviewsensei review status", new_run=True)
            self.assertNotEqual(f.invoke_command(), 0)
            self.assertEqual(f.comment["body"], saved_root)
            self.assertEqual(f.extra_comments, saved_parts)
            self.assertEqual(f.business_writes, 1)
            self.assertEqual(f.provider_calls, 1)
            self.assertFalse(any(review["state"] == "APPROVED" for review in f.reviews))

    def test_twenty_decisions_flow_through_current_command_and_finalization(self):
        from review_sensei_github.api_run_history import read_action_result
        from review_sensei_github.publication import parse_finding_index

        from review_sensei.bounded_evidence import MAX_MANIFEST_BYTES, canonical_bytes

        with tempfile.TemporaryDirectory() as directory:
            f = CommandEntryFixture(directory)
            f.provider_comments = [
                {
                    "path": "src/app.py",
                    "line": 2,
                    "side": "RIGHT",
                    "body": f"Confirm interface assumption {i}.",
                    "severity": "high",
                    "blocking": False,
                    "needs_human": True,
                }
                for i in range(20)
            ]
            self.assertEqual(f.invoke(), 0, f.error)
            original = parse_session_comment(f.comment["body"], identity=IDENTITY)
            index = parse_finding_index(
                f.reviews[-1]["body"], repository_id=123, pull_request=7, head_sha=HEAD
            )
            self.assertEqual(len(index.entries), 20)
            self.assertFalse(any(r["state"] == "APPROVED" for r in f.reviews))
            identifiers = [
                "RS-" + entry.instance[:6].upper() for entry in index.entries
            ]
            for start in (0, 10):
                f.SOURCE_ID += 1
                f.select_command(
                    "@reviewsensei override "
                    + " ".join(identifiers[start : start + 10])
                    + " acceptable risk",
                    new_run=True,
                )
                self.assertEqual(f.invoke_command(), 0, (f.error, f.exception))
                current = parse_session_comment(f.comment["body"], identity=IDENTITY)
                self.assertEqual(len(current.dispositions), start + 10)
                self.assertEqual(
                    current.transaction.transaction_id,
                    original.transaction.transaction_id,
                )
                self.assertEqual(
                    current.api_run_accounting["action"]["phase"], "terminal"
                )
                retained = read_action_result(f.selected.ledger, IDENTITY, current)
                self.assertEqual(retained["result"]["summary"] + "\n", f.stdout)
                self.assertEqual(len(retained["result"]["receipts"]), 10)
                self.assertEqual(len(retained["result"]["dispositions"]), 10)
                self.assertEqual(
                    {
                        receipt["reference"]
                        for receipt in retained["result"]["receipts"]
                    },
                    set(identifiers[start : start + 10]),
                )
                self.assertLessEqual(
                    len(
                        canonical_bytes(current.api_run_accounting["action"]["result"])
                    ),
                    MAX_MANIFEST_BYTES,
                )
                self.assertEqual(f.provider_calls, 1)
            self.assertEqual(sum(r["state"] == "APPROVED" for r in f.reviews), 1)
            self.assertEqual(len(f.resolved_threads), 20)
            decisions = copy.deepcopy(current.dispositions)
            f.SOURCE_ID += 1
            f.select_command("@reviewsensei verify", new_run=True)
            self.assertEqual(f.invoke_command(), 0, (f.error, f.exception))
            verified = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertEqual(verified.dispositions, decisions)
            self.assertEqual(verified.api_run_accounting["action"]["phase"], "terminal")
            self.assertEqual(sum(r["state"] == "APPROVED" for r in f.reviews), 1)
            self.assertEqual(f.provider_calls, 1)
            f.SOURCE_ID += 1
            f.select_command("@reviewsensei review pause", new_run=True)
            f.action_fault = "business-absent"
            self.assertNotEqual(f.invoke_command(), 0)
            refused = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertEqual(refused.dispositions, decisions)
            self.assertEqual(refused.api_run_accounting["action"]["phase"], "pending")
            refused_root = f.comment["body"]
            f.SOURCE_ID += 1
            f.select_command("@reviewsensei review status", new_run=True)
            self.assertNotEqual(f.invoke_command(), 0)
            self.assertEqual(f.comment["body"], refused_root)
            self.assertEqual(sum(r["state"] == "APPROVED" for r in f.reviews), 1)
            self.assertEqual(f.provider_calls, 1)


if __name__ == "__main__":
    unittest.main()
