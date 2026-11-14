"""Credential-free original entry, with only a fail-closed HTTP wire seam."""

import inspect
import io
import json
import os
import sys
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from fake_github_http import json_response
from test_review_run import fixture_process_environment


class OriginalWire:
    def __init__(self, fixture):
        self.fixture = fixture
        self.requests = []

    def open(self, request, *, timeout):
        if not isinstance(request, urllib.request.Request):
            raise AssertionError("original adapter requires an explicit request")
        target = urlsplit(request.full_url)
        if target.scheme != "https" or target.port not in (None, 443):
            raise AssertionError("unexpected original wire scheme or port")
        fixture = self.fixture
        self.requests.append((request.get_method(), request.full_url))
        if target.hostname == "ollama.com" and target.path == "/api/generate":
            assert request.get_method() == "POST"
            assert request.get_header("Authorization") == "Bearer synthetic-only"
            payload = json.loads(request.data)
            fixture.provider_calls += 1
            if fixture.before_provider:
                fixture.before_provider()
            content = {
                "summary": fixture.provider_summary or "Complete review.",
                "comments": fixture.provider_comments or [],
            }
            if hasattr(fixture, "provider_fully_reviewed"):
                content["fully_reviewed"] = fixture.provider_fully_reviewed
            body = json.dumps(
                {
                    "response": json.dumps(content),
                    "done": True,
                    "done_reason": "stop",
                    "eval_count": 10,
                    "model": payload["model"],
                }
            ).encode()
            return io.BytesIO(body)
        if (
            target.hostname == "github.reviewsensei.dev"
            and target.path == "/github/token"
        ):
            assert request.get_method() == "POST"
            payload = json.loads(request.data)
            assert (
                len(fixture.accounted.observations)
                == sum(attempt == fixture.attempt for attempt, _, _ in fixture.trace)
                + 1
            )
            fixture.trace.append((fixture.attempt, request.get_method(), target.path))
            fixture.worker_observations.append(
                {
                    "capability": payload["capability"],
                    "service_ceiling": payload["control_budget"]["max_dispatches"],
                    "observed_dispatches": min(
                        fixture.fanout, payload["control_budget"]["max_dispatches"]
                    ),
                }
            )
            return json_response(fixture.broker_response(payload))
        if target.hostname == "oidc.github.test" and target.path == "/token":
            return fixture.external(request, timeout)
        if target.hostname == "api.github.com" and (
            target.path == "/graphql"
            or target.path.startswith("/repos/owner/repo/")
            or target.path == "/repos/owner/repo"
            or target.path.startswith("/users/")
        ):
            return fixture.external(request, timeout)
        raise AssertionError("unexpected original wire URL: " + request.full_url)


def invoke_original(fixture):
    from review_sensei_github import cli, review_run
    from review_sensei_github.api_run_requests import ApiRunRequestTransport
    from review_sensei_github.http import GitHubHttp

    from review_sensei.cli import main as engine_main

    assert not fixture.legacy_publication_fixture
    assert fixture.active_finding_cap is None
    original_execute = inspect.unwrap(review_run.ReviewRun.execute)
    original_publish = inspect.unwrap(review_run.ReviewRunHooks.publish_review)
    original_begin = inspect.unwrap(review_run.ReviewRunHooks.begin_provider_phase)
    previous = sys.getprofile()
    wire = OriginalWire(fixture)

    def observe(frame, event, value):
        if (
            frame.f_code is ApiRunRequestTransport.__init__.__code__
            and event == "return"
        ):
            fixture.accounted = frame.f_locals["self"]
            fixture.transports.append(fixture.accounted)
        elif frame.f_code is GitHubHttp.__init__.__code__ and event == "return":
            fixture.http = frame.f_locals["self"]
        elif frame.f_code is original_execute.__code__ and event == "call":
            fixture.selected = frame.f_locals["self"]
        elif frame.f_code is original_publish.__code__ and event == "call":
            if fixture.before_publication:
                fixture.before_publication()
        elif frame.f_code is original_begin.__code__ and event == "return":
            if fixture.before_entry:
                fixture.before_entry()
        elif frame.f_code is engine_main.__code__ and event == "return":
            if fixture.after_engine:
                fixture.after_engine()
        if previous is not None:
            previous(frame, event, value)

    def external(_opener, request, data=None, timeout=None):
        assert data is None
        return wire.open(request, timeout=timeout)

    event_file = fixture.root / "event.json"
    if fixture.event_document is not None:
        event_file.write_text(json.dumps(fixture.event_document))
    environment = {
        **fixture_process_environment(),
        "GITHUB_RUN_ID": str(fixture.run_id),
        "REVIEW_SENSEI_WORKFLOW_REF": "malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@refs/tags/v5",
        "REVIEW_SENSEI_WORKFLOW_SHA": "a" * 40,
        "GITHUB_EVENT_NAME": fixture.event_name,
        "GITHUB_EVENT_PATH": str(event_file),
        "GITHUB_ACTOR": "alice",
        "GITHUB_TRIGGERING_ACTOR": "alice",
        "GITHUB_RUN_ATTEMPT": str(fixture.attempt),
        "GITHUB_TOKEN": "synthetic-workflow-read",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "synthetic",
        "ACTIONS_ID_TOKEN_REQUEST_URL": "https://oidc.github.test/token?fixture=1",
        "OLLAMA_API_KEY": "synthetic-only",
        "REVIEWSENSEI_APP_ID": "999",
    }
    argv = [
        "review-run",
        "--policy-commit",
        fixture.policy,
        "--run-directory",
        str(fixture.root),
        "--publication-outcome",
        str(fixture.root / "publication.json"),
        *(
            [
                "--quarantine-predecessor-comment-id",
                str(fixture.quarantine_predecessor_comment_id),
            ]
            if fixture.quarantine_predecessor_comment_id is not None
            else []
        ),
        "--",
        "--diff",
        str(fixture.root / "review.patch"),
        "--repository",
        "owner/repo",
        "--repository-id",
        "123",
        "--pull-request",
        "7",
        "--base-sha",
        fixture.requested_base,
        "--head-sha",
        fixture.requested_head,
        "--config",
        str(fixture.root / ".reviewsensei.yml"),
        "--review-mode",
        "merge-focused",
        "--no-learning-proposals",
        "--learning-root",
        str(fixture.root),
        *([fixture.review_intent] if fixture.review_intent else []),
        "--configuration-context-output",
        str(fixture.root / "configuration.json"),
        "--admission-context-output",
        str(fixture.root / "admission.json"),
    ]
    old = Path.cwd()
    errors = io.StringIO()
    try:
        os.chdir(fixture.root)
        with (
            patch.dict(os.environ, environment, clear=True),
            patch.object(urllib.request.OpenerDirector, "open", external),
            redirect_stdout(io.StringIO()),
            redirect_stderr(errors),
        ):
            sys.setprofile(observe)
            result = cli.main(argv)
        fixture.error = errors.getvalue()
        if hasattr(fixture, "run_results"):
            fixture.run_results[fixture.run_id, fixture.attempt] = result
            fixture.run_heads[fixture.run_id, fixture.attempt] = fixture.requested_head
        return result, wire
    finally:
        sys.setprofile(previous)
        os.chdir(old)
