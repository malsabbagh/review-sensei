"""Captured roots through real command entry, with synthetic external services.

The adapter reuses the existing fake service's owner/repo routing. It never
rewrites captured body strings or asks a live service to execute an old source.
Missing per-job timestamps are synthetic cessation controls, not incident proof.
"""

from __future__ import annotations

import copy
import io
import json
import os
import re
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request

from fake_github_http import json_response
from review_sensei_github import cli as package_cli
from review_sensei_github import review_run
from review_sensei_github.api_run_requests import ApiRunRequestTransport
from review_sensei_github.broker_client import BrokerClient
from review_sensei_github.http import GitHubHttp
from review_sensei_github.session_ledger import parse_session_comment
from test_action_run import CommandEntryFixture
from test_review_run import fixture_process_environment

from review_sensei.bounded_evidence import EvidenceReadBudget
from review_sensei.session import SessionIdentity


class _IncidentClock(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 11, 1, 15, 0, tzinfo=timezone.utc)
        return value if tz is None else value.astimezone(tz)


class IncidentCommandFixture(CommandEntryFixture):
    def __init__(self, directory, incident):
        super().__init__(directory, limit=512)
        self.incident = incident
        self.repository = incident["repository"]
        self.repository_id = incident["repository_id"]
        self.pull_request = incident["pull_request"]
        self.identity = SessionIdentity(
            self.repository, self.pull_request, repository_id=self.repository_id
        )
        self.comment = copy.deepcopy(incident["comment"])
        self.comment["user"] = {**self.comment["user"], "type": "Bot"}
        self.source = copy.deepcopy(incident["source"])
        self.source["user"] = {**self.source["user"], "type": "User"}
        self.source["author_association"] = "OWNER"
        self.source["issue_url"] = (
            f"https://api.github.test/repos/{self.repository}/issues/{self.pull_request}"
        )
        self.SOURCE_ID = self.source["id"]
        self.SOURCE_STAMP = self.source["updated_at"]
        self.command_body = self.source["body"]
        self.command_active = True
        self.owner_job = "command"
        self.event_name = "issue_comment"
        self.run_id = incident["command_run_id"]
        self.requested_head = self.live_head = incident["expected"]["head_sha"]
        self.requested_base = self.live_base = incident["expected"]["base_sha"]
        self.native_mutations = []
        self.synthetic_base_ref = "main"
        self.synthetic_cessation_time = "2026-01-01T00:00:00Z"

    def _actor(self):
        return {
            "login": self.source["user"]["login"],
            "type": "User",
            "association": "OWNER",
        }

    def _expected_authority(self):
        return {
            **super()._expected_authority(),
            "repository": self.repository,
            "repository_id": self.repository_id,
            "pull_request": self.pull_request,
        }

    def _response_subject(self, value):
        if isinstance(value, list):
            return [self._response_subject(item) for item in value]
        if not isinstance(value, dict):
            return value
        result = {key: self._response_subject(item) for key, item in value.items()}
        if value.get("full_name") == "owner/repo":
            result.update(full_name=self.repository, id=self.repository_id)
        if value.get("repository") == "owner/repo":
            result["repository"] = self.repository
        if value.get("repository_id") == 123:
            result["repository_id"] = self.repository_id
        if value.get("pull_request") == 7:
            result["pull_request"] = self.pull_request
        if value.get("number") == 7 and "base" in value and "head" in value:
            result["number"] = self.pull_request
            result["base"]["ref"] = self.synthetic_base_ref
        return result

    def external(self, request, timeout):
        parsed = urlsplit(request.full_url)
        method, path = request.get_method(), parsed.path
        if parsed.hostname == "broker.github.test":
            return self._broker_response(request)
        if method != "GET":
            self.native_mutations.append((method, path))
            raise AssertionError("incident replay must not dispatch native mutations")
        old_run = self.incident["predecessor_run_id"]
        if f"/actions/runs/{old_run}" in path:
            self._meter(request)
            if path.endswith("/jobs"):
                jobs = copy.deepcopy(self.incident["predecessor_jobs"])
                for job in jobs:
                    job["run_attempt"] = 1
                    job.setdefault("completed_at", self.synthetic_cessation_time)
                return json_response({"jobs": jobs, "total_count": len(jobs)})
            return json_response(
                {
                    "id": old_run,
                    "run_attempt": 1,
                    "repository": {
                        "id": self.repository_id,
                        "full_name": self.repository,
                    },
                    "status": "completed",
                    "conclusion": "failure",
                    "event": "pull_request",
                    "head_sha": self.incident["expected"]["head_sha"],
                }
            )
        alias = path.replace("/repos/" + self.repository, "/repos/owner/repo")
        alias = re.sub(
            rf"(/(?:pulls|issues)/){self.pull_request}(?=/|$)", r"\g<1>7", alias
        )
        alias = alias.replace(
            "/collaborators/" + self.source["user"]["login"] + "/permission",
            "/collaborators/alice/permission",
        )
        routed = Request(
            urlunsplit(
                (parsed.scheme, parsed.netloc, alias, parsed.query, parsed.fragment)
            ),
            data=request.data,
            headers=dict(request.headers),
            method=method,
        )
        response = super().external(routed, timeout)
        return json_response(
            self._response_subject(json.loads(response.read(524288))),
            getattr(response, "status", 200),
        )

    def invoke_incident(self):
        event_file = self.root / "captured-command-event.json"
        event_file.write_text(
            json.dumps(
                {
                    "issue": {"number": self.pull_request, "pull_request": {}},
                    "comment": self.source,
                }
            )
        )

        def caller_factory(**kwargs):
            self.accounted = ApiRunRequestTransport(**kwargs)
            self.transports.append(self.accounted)
            return self.accounted

        def broker_factory(**kwargs):
            return BrokerClient(
                broker_url="https://broker.github.test/api/github/token", **kwargs
            )

        def http_factory(**kwargs):
            self.http = GitHubHttp(api_url="https://api.github.test", **kwargs)
            return self.http

        environment = {
            **fixture_process_environment(),
            "GITHUB_RUN_ID": str(self.run_id),
            "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_EVENT_NAME": "issue_comment",
            "GITHUB_EVENT_PATH": str(event_file),
            "REVIEW_SENSEI_WORKFLOW_REF": "malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@refs/tags/v5",
            "REVIEW_SENSEI_WORKFLOW_SHA": "a" * 40,
            "GITHUB_ACTOR": self.source["user"]["login"],
            "GITHUB_TRIGGERING_ACTOR": self.source["user"]["login"],
            "GITHUB_TOKEN": "synthetic-workflow-read",
            "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "synthetic",
            "ACTIONS_ID_TOKEN_REQUEST_URL": "https://oidc.github.test/token?fixture=1",
            "ARTIFACTS": "none",
        }
        output, errors = io.StringIO(), io.StringIO()
        old_directory = Path.cwd()
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
                    patch("review_sensei.session.datetime", _IncidentClock),
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
                        self.repository,
                        "--repository-id",
                        str(self.repository_id),
                        "--pull-request",
                        str(self.pull_request),
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
                    ]
                )
        finally:
            os.chdir(old_directory)
            self.stdout, self.error = output.getvalue(), errors.getvalue()

    def recorded(self):
        return parse_session_comment(self.comment["body"], identity=self.identity)
