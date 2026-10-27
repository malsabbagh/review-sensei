"""Single review owner: distinct policy and reviewed revisions, no file authority."""

from __future__ import annotations

import tempfile
import unittest
from argparse import Namespace
from dataclasses import replace
from pathlib import Path

from review_sensei_github.api_run_host import ApiRunReviewHooks
from review_sensei_github.api_run_requests import ApiRunSessionLedger
from test_api_work_lifecycle import QUALIFICATION_LIMIT, LifecycleFixture
from test_api_work_persistence import BASE, HEAD, IDENTITY


def fixture_process_environment():
    """Keep executable discovery and platform runtime fields, without credentials."""
    import os

    names = {"PATH", "SYSTEMROOT", "COMSPEC", "WINDIR", "TMP", "TEMP", "TMPDIR", "LANG"}
    return {key: value for key, value in os.environ.items() if key.upper() in names}


class ReviewRunPolicyTests(unittest.TestCase):
    def test_owner_allocation_conserves_closed_integer_limit(self):
        from review_sensei_github.api_run_plan import owner_allocations

        from review_sensei.api_work import MAX_API_REQUEST_LIMIT
        from review_sensei.errors import ReviewInputError

        for limit in (512, 1024, MAX_API_REQUEST_LIMIT):
            with self.subTest(limit=limit):
                allocation = owner_allocations(limit, 130)
                self.assertEqual(
                    130 + allocation.first_total + allocation.successor_total,
                    limit,
                )
        for limit, startup in (
            (True, 130),
            (512.0, 130),
            (MAX_API_REQUEST_LIMIT + 1, 130),
            (512, MAX_API_REQUEST_LIMIT + 1),
            (512, True),
            (512, -1),
        ):
            with self.subTest(limit=limit, startup=startup):
                with self.assertRaises(ReviewInputError):
                    owner_allocations(limit, startup)

    def test_frozen_policy_commit_is_independent_from_reviewed_base(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LifecycleFixture(directory)
            native, configuration, _, owner = fixture.prelude()
            policy = replace(configuration, commit="c" * 40)
            fixture.accounted.initialize_and_activate(native, IDENTITY, policy, owner)
            ledger = ApiRunSessionLedger(native, fixture.accounted, IDENTITY)
            hook = ApiRunReviewHooks(ledger, IDENTITY, base_sha=BASE, head_sha=HEAD)
            config = Path(directory) / "trusted.yml"
            config.write_bytes(fixture.config_bytes)
            args = Namespace(
                repository=IDENTITY.repository,
                repository_id=IDENTITY.repository_id,
                pull_request=IDENTITY.pull_request,
                base_sha=BASE,
                config=config,
            )
            before = len(fixture.trace)
            self.assertIs(hook.resolve_ledger(args, resolved_head_sha=HEAD), ledger)
            self.assertEqual(len(fixture.trace), before)
            self.assertEqual(fixture.accounted.accounting.configuration, policy)


class EntryFixture(LifecycleFixture):
    """Reuse the accepted real publisher/ledger fixture, adding policy startup."""

    def __init__(self, directory, *, limit=512, runner_kind="hosted"):
        import subprocess

        import test_review_transaction as tx

        super().__init__(directory, limit=limit)
        self.root = Path(directory)
        self.owner_job = runner_kind
        backend = "cloud-ollama" if runner_kind == "hosted" else "local-ollama"
        model = "deepseek-v4.1-flash:cloud" if runner_kind == "hosted" else "qwen3.5:4b"
        self.config_bytes = (
            f"schema: 1\napi_request_limit: {limit}\ninference:\n  backend: {backend}\n  model: {model}\ngithub:\n  writes: true\n  publication_inventory: complete\n"
        ).encode()
        (self.root / ".reviewsensei.yml").write_bytes(self.config_bytes)
        subprocess.run(["git", "init", str(self.root)], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(self.root), "add", ".reviewsensei.yml"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(self.root),
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-m",
                "fixture",
            ],
            check=True,
            capture_output=True,
        )
        self.policy = subprocess.check_output(
            ["git", "-C", str(self.root), "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.PIPE,
        ).strip()
        self.policy_ref = self.policy
        (self.root / "review.patch").write_text(
            tx.HostedAnalysisCliTests.ANALYSIS_DIFF, encoding="utf-8", newline="\n"
        )
        self.live_head = HEAD
        self.live_base = BASE
        self.requested_head = HEAD
        self.requested_base = BASE
        self.before_provider = None
        self.before_entry = None
        self.after_engine = None
        self.before_publication = None
        self.selected = None
        self.provider_calls = 0
        self.provider_comments = None
        self.provider_summary = None
        self.event_name = "pull_request"
        self.event_document = None
        self.review_intent = None
        self.quarantine_predecessor_comment_id = None
        self.overlap = False
        self.refuse_broker = False
        self.run_id = 456
        self.previous_active = False
        self.previous_incomplete = False
        self.previous_reads = 0
        self.rollover_fault = None
        self.change_root = False
        self.archive_bad = None
        self.abort_fault = None

    def external(self, request, timeout):
        import base64
        import json
        from urllib.parse import urlsplit

        from fake_github_http import json_response

        path = urlsplit(request.full_url).path
        if path.endswith("/collaborators/alice/permission"):
            self.trace.append((self.attempt, request.get_method(), path))
            return json_response({"permission": "maintain"})
        if path == "/api/tags":
            assert request.get_header("Authorization") is None
            self.trace.append((self.attempt, request.get_method(), path))
            return json_response({"models": [{"name": "qwen3.5:4b"}]})
        policy = (
            path in ("/repos/owner/repo", "/repos/owner/repo/git/ref/heads/main")
            or "/contents/.reviewsensei.yml" in path
        )
        if policy:
            assert (
                len(self.accounted.observations)
                == sum(a == self.attempt for a, _, _ in self.trace) + 1
            )
            self.trace.append((self.attempt, request.get_method(), path))
            if path.endswith("/main"):
                return json_response(
                    {"object": {"type": "commit", "sha": self.policy_ref}}
                )
            if "/contents/" in path:
                return json_response(
                    {
                        "type": "file",
                        "encoding": "base64",
                        "size": len(self.config_bytes),
                        "content": base64.b64encode(self.config_bytes).decode(),
                    }
                )
            return json_response(
                {
                    "id": 123,
                    "full_name": "owner/repo",
                    "fork": False,
                    "default_branch": "main",
                }
            )
        if (
            self.refuse_broker
            and urlsplit(request.full_url).hostname == "broker.github.test"
        ):
            self.trace.append((self.attempt, request.get_method(), path))
            return json_response({"error": "synthetic refusal"}, 401)
        if "/actions/runs/" in path:
            import re

            self.trace.append((self.attempt, request.get_method(), path))
            run_id = int(path.split("/actions/runs/")[1].split("/")[0])
            current = run_id == self.run_id
            active = current or self.previous_active
            if path.endswith("/jobs"):
                attempt = int(re.search(r"/attempts/(\d+)/", path)[1])
                active = current and attempt == self.attempt or self.previous_active
                if not current:
                    self.previous_reads += 1
                    if self.change_root and self.previous_reads == 2:
                        self.rewrite_record(generation_delta=1)
                return json_response(
                    {
                        "total_count": 2
                        if not current and self.previous_incomplete
                        else 1,
                        "jobs": [
                            {
                                "id": run_id * 10 + attempt,
                                "run_id": run_id,
                                "run_attempt": attempt,
                                "name": "ReviewSensei / " + self.owner_job,
                                "status": "in_progress" if active else "completed",
                                "conclusion": None if active else "success",
                                "completed_at": None
                                if active
                                else "2026-10-26T00:00:00Z",
                            }
                        ]
                        + (
                            [
                                {
                                    "id": 9999,
                                    "run_id": run_id,
                                    "run_attempt": attempt,
                                    "name": "ReviewSensei / command",
                                    "status": "in_progress",
                                }
                            ]
                            if self.overlap
                            else []
                        ),
                    }
                )
            return json_response(
                {
                    "id": run_id,
                    "run_attempt": self.attempt,
                    "repository": {"id": 123, "full_name": "owner/repo"},
                    "status": "in_progress" if active else "completed",
                    "event": self.event_name if current else "pull_request",
                }
            )
        payload = json.loads(request.data) if request.data else {}
        if self.abort_fault and request.get_method() == "PATCH":
            from review_sensei_github.session_ledger import parse_session_comment

            candidate = parse_session_comment(payload["body"], identity=IDENTITY)
            closing = candidate.api_run_accounting["analysis_abort"] is not None
            cleaning = (
                not closing
                and candidate.transaction is None
                and self.comment is not None
                and parse_session_comment(
                    self.comment["body"], identity=IDENTITY
                ).transaction
                is not None
            )
            if (
                closing
                and self.abort_fault.startswith("closure")
                or cleaning
                and self.abort_fault.startswith("cleanup")
            ):
                fault, self.abort_fault = self.abort_fault, None
                if fault.endswith("stored"):
                    self._external_open(request, timeout)
                else:
                    self.trace.append((self.attempt, request.get_method(), path))
                raise TimeoutError("synthetic lost abort response")
        archive = request.get_method() == "POST" and payload.get("body", "").startswith(
            "ReviewSensei immutable evidence part v1"
        )
        transition = request.get_method() == "PATCH" and self.accounted._state is None
        fault = self.rollover_fault
        loses = fault and (
            archive
            and fault.startswith("archive")
            or transition
            and fault.startswith("transition")
        )
        if loses:
            self.rollover_fault = None
            if fault.endswith("stored"):
                self._external_open(request, timeout)
            else:
                self.trace.append((self.attempt, request.get_method(), path))
            raise TimeoutError("synthetic uncertain rollover effect")
        response = self._external_open(request, timeout)
        if (
            self.archive_bad
            and request.get_method() == "GET"
            and "/issues/comments/" in path
        ):
            value = json.loads(response.read(524288))
            if value.get("body", "").startswith(
                "ReviewSensei immutable evidence part v1"
            ):
                if self.archive_bad == "owner":
                    value["user"]["id"] = 998
                else:
                    value["body"] += "tampered"
            return json_response(value)
        if path.endswith("/pulls/7"):
            pull = json.loads(response.read(524288))
            pull["head"]["sha"] = self.live_head
            pull["head"]["repo"]["id"] = 123
            pull["base"]["sha"] = self.live_base
            return json_response(pull)
        if path.endswith("/actions/runs/456"):
            run = json.loads(response.read(524288))
            run["event"] = "pull_request"
            return json_response(run)
        if path.endswith("/jobs"):
            jobs = json.loads(response.read(524288))
            jobs["jobs"][0]["name"] = "ReviewSensei / " + self.owner_job
            if not self.overlap:
                return json_response(jobs)
            jobs["jobs"].append(
                {**jobs["jobs"][0], "id": 9999, "name": "ReviewSensei / command"}
            )
            jobs["total_count"] += 1
            return json_response(jobs)
        return response

    def rewrite_record(self, *, generation_delta=0, mutate=None):
        from review_sensei_github.session_ledger import (
            parse_session_comment,
            render_session_comment,
        )

        record = parse_session_comment(self.comment["body"], identity=IDENTITY)
        updated = (
            mutate(record)
            if mutate
            else record.evolve(generation=record.generation + generation_delta)
        )
        self.comment["body"] = render_session_comment(
            repository_id=123, pull_request=7, record=updated
        )

    def next_run(self):
        self.run_id += 1
        self.live_head = self.requested_head = "d" * 40
        self.trace.clear()
        self.transports.clear()
        self.worker_observations.clear()

    def invoke(self, *, human=False):
        import io
        import json
        import os
        from contextlib import ExitStack, redirect_stderr, redirect_stdout
        from unittest.mock import patch

        import test_review_transaction as tx
        from review_sensei_github import cli as package_cli
        from review_sensei_github import review_run
        from review_sensei_github.api_run_requests import ApiRunRequestTransport
        from review_sensei_github.broker_client import BrokerClient
        from review_sensei_github.http import GitHubHttp

        from review_sensei.bounded_evidence import EvidenceReadBudget
        from review_sensei.models import ProviderResponse

        fixture = self
        event_file = self.root / "event.json"
        if self.event_document is not None:
            event_file.write_text(json.dumps(self.event_document))

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

        class Provider(tx.HostedAnalysisCliTests._Provider):
            # Fake only completion HTTP, while preserving the selected adapter
            # identity so independent successor context reconstruction is real.
            name = "ollama"
            model = (
                "deepseek-v4.1-flash:cloud"
                if fixture.owner_job == "hosted"
                else "qwen3.5:4b"
            )

            def complete(self, request):
                fixture.provider_calls += 1
                if fixture.before_provider:
                    fixture.before_provider()
                comments = (
                    []
                    if not human
                    else [
                        {
                            "path": "src/app.py",
                            "line": 2,
                            "side": "RIGHT",
                            "body": "Confirm the external caller contract for this change.",
                            "severity": "high",
                            "blocking": False,
                            "needs_human": True,
                        }
                    ]
                )
                if fixture.provider_comments is not None:
                    comments = fixture.provider_comments
                return ProviderResponse(
                    text=json.dumps(
                        {
                            "summary": fixture.provider_summary or "Complete review.",
                            "comments": comments,
                        }
                    ),
                    provider=self.name,
                    model=self.model,
                )

        original_engine = review_run.engine_main
        original_execute = review_run.ReviewRun.execute
        original_publish = review_run.ReviewRunHooks.publish_review
        original_begin = review_run.ReviewRunHooks.begin_provider_phase

        def engine(*args, **kwargs):
            result = original_engine(*args, **kwargs)
            if fixture.after_engine:
                fixture.after_engine()
            return result

        def execute(selected, *args, **kwargs):
            fixture.selected = selected
            return original_execute(selected, *args, **kwargs)

        def publish(hooks, *args, **kwargs):
            if fixture.before_publication:
                fixture.before_publication()
            return original_publish(hooks, *args, **kwargs)

        def begin(hooks, *args, **kwargs):
            result = original_begin(hooks, *args, **kwargs)
            if fixture.before_entry:
                fixture.before_entry()
            return result

        old = Path.cwd()
        errors = io.StringIO()
        try:
            os.chdir(self.root)
            with ExitStack() as stack:
                for scope in (
                    patch.dict(
                        os.environ,
                        {
                            **fixture_process_environment(),
                            "GITHUB_RUN_ID": str(self.run_id),
                            "REVIEW_SENSEI_WORKFLOW_REF": "malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@refs/tags/v5",
                            "REVIEW_SENSEI_WORKFLOW_SHA": "a" * 40,
                            "GITHUB_EVENT_NAME": self.event_name,
                            "GITHUB_EVENT_PATH": str(event_file),
                            "GITHUB_ACTOR": "alice",
                            "GITHUB_TRIGGERING_ACTOR": "alice",
                            "GITHUB_RUN_ATTEMPT": str(self.attempt),
                            "GITHUB_TOKEN": "synthetic-workflow-read",
                            "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "synthetic",
                            "ACTIONS_ID_TOKEN_REQUEST_URL": "https://oidc.github.test/token?fixture=1",
                            "OLLAMA_API_KEY": "synthetic-only",
                            "REVIEWSENSEI_APP_ID": "999",
                        },
                        clear=True,
                    ),
                    patch.object(review_run, "physical_opener", self.external),
                    patch.object(
                        review_run, "ApiRunRequestTransport", side_effect=caller_factory
                    ),
                    patch.object(
                        review_run, "BrokerClient", side_effect=broker_factory
                    ),
                    patch.object(review_run, "GitHubHttp", side_effect=http_factory),
                    patch.object(review_run, "engine_main", side_effect=engine),
                    patch.object(review_run.ReviewRun, "execute", execute),
                    patch.object(review_run.ReviewRunHooks, "publish_review", publish),
                    patch.object(
                        review_run.ReviewRunHooks, "begin_provider_phase", begin
                    ),
                    patch(
                        "review_sensei.cli.default_registry",
                        return_value=tx.HostedAnalysisCliTests._Registry(Provider()),
                    ),
                    patch.object(
                        EvidenceReadBudget,
                        "__init__",
                        side_effect=AssertionError("no phase budget"),
                    ),
                    redirect_stdout(io.StringIO()),
                    redirect_stderr(errors),
                ):
                    stack.enter_context(scope)
                result = package_cli.main(
                    [
                        "review-run",
                        "--policy-commit",
                        self.policy,
                        "--run-directory",
                        str(self.root),
                        "--publication-outcome",
                        str(self.root / "publication.json"),
                        *(
                            [
                                "--quarantine-predecessor-comment-id",
                                str(self.quarantine_predecessor_comment_id),
                            ]
                            if self.quarantine_predecessor_comment_id is not None
                            else []
                        ),
                        "--",
                        "--diff",
                        str(self.root / "review.patch"),
                        "--repository",
                        "owner/repo",
                        "--repository-id",
                        "123",
                        "--pull-request",
                        "7",
                        "--base-sha",
                        self.requested_base,
                        "--head-sha",
                        self.requested_head,
                        "--config",
                        str(self.root / ".reviewsensei.yml"),
                        "--review-mode",
                        "merge-focused",
                        "--no-learning-proposals",
                        "--learning-root",
                        str(self.root),
                        *([self.review_intent] if self.review_intent else []),
                        "--configuration-context-output",
                        str(self.root / "configuration.json"),
                        "--admission-context-output",
                        str(self.root / "admission.json"),
                    ]
                )
            self.error = errors.getvalue()
            return result
        finally:
            os.chdir(old)


class _CleanupTailFixture(EntryFixture):
    def __init__(self, directory, mode, **kwargs):
        super().__init__(directory, **kwargs)
        self.mode = mode
        self.threads = []
        self.mutations = []
        self.graphql_operations = []

    def external(self, request, timeout):
        import copy
        import json
        from urllib.parse import urlsplit

        from fake_github_http import json_response

        path = urlsplit(request.full_url).path
        payload = json.loads(request.data) if request.data else None
        if path == "/graphql":
            self.trace.append((self.attempt, request.method, path))
            operation = payload.get("operationName")
            self.graphql_operations.append(operation)
            if operation == "ResolveReviewThread":
                identifier = payload["variables"]["input"]["threadId"]
                self.mutations.append(identifier)
                node = next(n for n in self.threads if n["id"] == identifier)
                if self.mode not in {"refused", "lost-unstored"}:
                    node["isResolved"] = True
                if self.mode in {"lost-ack", "lost-unstored"}:
                    raise TimeoutError("synthetic lost cleanup acknowledgement")
                if self.mode != "confirmed":
                    return json_response({}, 500)
                return json_response(
                    {
                        "data": {
                            "resolveReviewThread": {
                                "thread": {"id": identifier, "isResolved": True}
                            }
                        }
                    }
                )
            if operation == "ResolutionReadback":
                identifier = payload["variables"]["id"]
                node = next(n for n in self.threads if n["id"] == identifier)
                return json_response(
                    {
                        "data": {
                            "node": {"id": identifier, "isResolved": node["isResolved"]}
                        }
                    }
                )
            return json_response(
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "reviewThreads": {
                                    "nodes": copy.deepcopy(self.threads),
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
        if "/pulls/comments/" in path:
            self.trace.append((self.attempt, request.method, path))
            identifier = int(path.rsplit("/", 1)[1])
            item = next(x for x in self.inline if x["id"] == identifier)
            if request.method == "PATCH":
                item["body"] = payload["body"]
            return json_response(copy.deepcopy(item))
        response = super().external(request, timeout)
        if path.endswith("/jobs") and self.overlap:
            value = json.loads(response.read(524288))
            value["total_count"] = len(value["jobs"])
            return json_response(value)
        if path.endswith("/pulls/7/reviews") and request.method == "POST":
            # The reusable fixture hardcodes its initial head for inline rows;
            # return the submitted head on this evolving synthetic server.
            for item in self.inline:
                if item["pull_request_review_id"] == self.reviews[-1]["id"]:
                    item["commit_id"] = payload["commit_id"]
                    self.threads.append(
                        {
                            "id": "LIVE_" + str(item["id"]),
                            "isResolved": False,
                            "comments": {
                                "nodes": [
                                    {
                                        "databaseId": item["id"],
                                        "body": item["body"],
                                        "author": {
                                            "login": "reviewsensei",
                                            "__typename": "Bot",
                                        },
                                    }
                                ]
                            },
                        }
                    )
        return response


class _CompletionFixture(_CleanupTailFixture):
    """Native metadata from external HTTP, with the real completion path intact."""

    def __init__(self, directory, mode="lost-ack", **kwargs):
        super().__init__(directory, mode, **kwargs)
        self.completion_fault = None
        self.completion_thread_reads = 0
        self.run_results = {}
        self.run_heads = {}
        self.terminal_write_fault = False

    def invoke(self, *, human=False):
        result = super().invoke(human=human)
        self.run_results[self.run_id, self.attempt] = result
        self.run_heads[self.run_id, self.attempt] = self.requested_head
        return result

    def external(self, request, timeout):
        import copy
        import json
        from urllib.parse import urlsplit

        from fake_github_http import json_response

        path = urlsplit(request.full_url).path
        payload = json.loads(request.data) if request.data else {}
        if (
            self.terminal_write_fault
            and request.method == "PATCH"
            and "/issues/comments/" in path
        ):
            from review_sensei_github.session_ledger import parse_session_comment

            record = parse_session_comment(payload["body"], identity=IDENTITY)
            if (
                record is not None
                and record.api_run_accounting["terminal_transaction"] is not None
            ):
                self.trace.append((self.attempt, request.method, path))
                raise TimeoutError("synthetic uncertain terminal-marker write")
        operation = payload.get("operationName")
        if path.endswith("/rules/branches/main"):
            self.trace.append((self.attempt, request.method, path))
            return json_response([])
        if path.endswith("/branches/main/protection"):
            self.trace.append((self.attempt, request.method, path))
            return json_response({}, 404)
        if path == "/graphql" and operation in {
            "ReviewCompletionThreads",
            "ReviewCompletionReviewDates",
        }:
            self.trace.append((self.attempt, request.method, path))
            self.graphql_operations.append(operation)
            if operation == "ReviewCompletionReviewDates":
                rows = []
                for identifier in payload["variables"]["ids"]:
                    review = next(r for r in self.reviews if r["node_id"] == identifier)
                    rows.append(
                        {
                            "databaseId": review["id"],
                            "createdAt": review["submitted_at"],
                            "updatedAt": review["submitted_at"],
                            "lastEditedAt": "2026-10-26T23:59:00Z"
                            if self.completion_fault == "edited"
                            and review is self.reviews[0]
                            else None,
                            "commit": {"oid": review["commit_id"]},
                        }
                    )
                return json_response({"data": {"nodes": rows}})
            nodes = copy.deepcopy(self.threads)
            self.completion_thread_reads += 1
            if self.completion_fault == "missing-root":
                nodes = nodes[1:]
            if (
                self.completion_fault == "closing-change"
                and self.completion_thread_reads > 1
            ):
                nodes[0]["isResolved"] = False
            return json_response(
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "headRefOid": self.live_head,
                                "baseRefOid": getattr(self, "live_base", BASE),
                                "reviewThreads": {
                                    "nodes": nodes,
                                    "pageInfo": {
                                        "hasNextPage": self.completion_fault
                                        == "pagination",
                                        "endCursor": None,
                                    },
                                },
                            }
                        }
                    }
                }
            )
        if request.method == "GET" and "/check-runs" in path:
            self.trace.append((self.attempt, request.method, path))
            rows = copy.deepcopy([c for c in self.check_runs if c["head_sha"] in path])
            if self.completion_fault == "check":
                rows[0]["conclusion"] = "success"
            if self.completion_fault == "check-producer":
                rows[0]["app"]["slug"] = "another-app"
            return json_response(
                {
                    "check_runs": rows,
                    "total_count": len(rows)
                    + (self.completion_fault == "check-incomplete"),
                }
            )
        if (
            request.method == "GET"
            and path.endswith("/pulls/7/reviews")
            and self.completion_fault == "budget"
        ):
            from urllib.parse import parse_qs

            query = parse_qs(urlsplit(request.full_url).query)
            page, size = int(query["page"][0]), int(query["per_page"][0])
            self.trace.append((self.attempt, request.method, path))
            return json_response(
                [
                    {
                        "id": page * 1000 + i,
                        "user": {"id": 888, "type": "User", "login": "alice"},
                    }
                    for i in range(size)
                ]
            )
        if (
            request.method == "GET"
            and "/reviews/" in path
            and not path.endswith("/comments")
        ):
            self.trace.append((self.attempt, request.method, path))
            identifier = int(path.rsplit("/", 1)[1])
            return json_response(
                copy.deepcopy(next(r for r in self.reviews if r["id"] == identifier))
            )
        if (
            request.method == "GET"
            and "/reviews/" in path
            and path.endswith("/comments")
        ):
            self.trace.append((self.attempt, request.method, path))
            identifier = int(path.split("/reviews/")[1].split("/")[0])
            return json_response(
                copy.deepcopy(
                    [
                        c
                        for c in self.inline
                        if c["pull_request_review_id"] == identifier
                    ]
                )
            )
        response = super().external(request, timeout)
        if "/actions/runs/" in path:
            from review_sensei.historical_completion import QUALIFIED_EXECUTORS

            value = json.loads(response.read(524288))
            run_id = int(path.split("/actions/runs/")[1].split("/")[0])
            attempt = (
                int(path.split("/attempts/")[1].split("/")[0])
                if "/attempts/" in path
                else self.attempt
            )
            result = self.run_results.get((run_id, attempt), 0)
            conclusion = "success" if result == 0 else "failure"
            current = run_id == self.run_id and attempt == self.attempt
            if not current and self.completion_fault in {
                "failed-owner",
                "cancelled-owner",
            }:
                conclusion = (
                    "failure"
                    if self.completion_fault == "failed-owner"
                    else "cancelled"
                )
            job_kind = getattr(self, "past_jobs", {}).get(run_id, self.owner_job)
            if path.endswith("/jobs"):
                value["jobs"][0]["name"] = "ReviewSensei / " + job_kind
                for job in value["jobs"]:
                    if job["name"].endswith(" / " + job_kind):
                        if not current:
                            job["conclusion"] = conclusion
                        job["steps"] = [
                            {
                                "number": 1,
                                "name": f"Run {job_kind} review",
                                "status": "in_progress" if current else "completed",
                                "conclusion": None
                                if current
                                else (
                                    "failure"
                                    if self.completion_fault == "masked-step-failure"
                                    else conclusion
                                ),
                                "completed_at": None
                                if current
                                else job["completed_at"],
                            },
                            {
                                "number": 2,
                                "name": f"Generate and publish {job_kind} mention reply",
                                "status": "completed",
                                "conclusion": "skipped",
                                "completed_at": None,
                            },
                        ]
                for kind in {"hosted", "local", "command"} - {job_kind}:
                    if not any(
                        job["name"].endswith(" / " + kind) for job in value["jobs"]
                    ):
                        value["jobs"].append(
                            {
                                "id": run_id * 10 + 100 + len(value["jobs"]),
                                "run_id": run_id,
                                "run_attempt": attempt,
                                "name": "ReviewSensei / " + kind,
                                "status": "completed",
                                "conclusion": "skipped",
                                "completed_at": None,
                                "steps": [],
                            }
                        )
                value["total_count"] = len(value["jobs"])
            else:
                key = next(iter(QUALIFIED_EXECUTORS))
                contract = QUALIFIED_EXECUTORS[key]
                value.update(
                    run_attempt=attempt,
                    conclusion=None if current else conclusion,
                    status="in_progress"
                    if current or self.previous_active
                    else "completed",
                    head_sha=self.run_heads.get((run_id, attempt), self.requested_head),
                    referenced_workflows=[
                        {
                            "path": contract["workflow_path"],
                            "ref": contract["workflow_ref"],
                            "sha": "0" * 40
                            if self.completion_fault == "unknown-source"
                            else key,
                        }
                    ],
                )
            return json_response(value)
        if path.endswith("/pulls/7") and hasattr(self, "live_base"):
            value = json.loads(response.read(524288))
            value["base"]["sha"] = self.live_base
            return json_response(value)
        if (
            request.method == "POST"
            and path.endswith("/issues/7/comments")
            and payload.get("body", "").startswith(
                "ReviewSensei immutable evidence part v1"
            )
        ):
            if self.completion_fault == "base-after-archive":
                self.live_base = "9" * 40
            if self.completion_fault == "root-after-archive":
                self.rewrite_record(generation_delta=1)
        if request.method == "POST" and path.endswith("/pulls/7/reviews"):
            review = self.reviews[-1]
            review["node_id"] = "REVIEW_" + str(review["id"])
            review["submitted_at"] = f"2026-10-25T22:00:{review['id'] - 100:02d}Z"
            for comment in self.inline:
                if comment["pull_request_review_id"] == review["id"]:
                    comment["created_at"] = comment["updated_at"] = review[
                        "submitted_at"
                    ]
            for thread in self.threads:
                root = thread["comments"]["nodes"][0]
                comment = next(c for c in self.inline if c["id"] == root["databaseId"])
                root["createdAt"] = comment["created_at"]
                root["updatedAt"] = comment["updated_at"]
            return json_response(copy.deepcopy(review))
        if request.method == "POST" and path.endswith("/check-runs"):
            self.check_runs[-1]["app"]["id"] = 77
            return json_response(copy.deepcopy(self.check_runs[-1]), 201)
        return response


class ReviewRunEntryTests(unittest.TestCase):
    def test_hard_killed_pending_result_has_one_proven_successor_and_no_reinference(
        self,
    ):
        import json
        import os
        import subprocess
        import sys

        from review_sensei_github.session_ledger import parse_session_comment

        from review_sensei.api_work import ApiRunAccounting

        for active in (True, False):
            with (
                self.subTest(active=active),
                tempfile.TemporaryDirectory() as directory,
            ):
                f = EntryFixture(directory, limit=512)
                child_root = f.root / "child"
                child_root.mkdir()
                packet = f.root / "killed.json"
                script = """
import json, os, sys
from pathlib import Path
from test_review_run import EntryFixture
f = EntryFixture(sys.argv[1], limit=512)
def killed():
    Path(sys.argv[2]).write_text(json.dumps({"comment":f.comment,"extra":f.extra_comments,"policy":f.policy,"provider_calls":f.provider_calls}))
    os._exit(91)
f.before_publication = killed
f.invoke()
os._exit(92)
"""
                process = subprocess.run(
                    [sys.executable, "-c", script, str(child_root), str(packet)],
                    capture_output=True,
                    timeout=20,
                    env={
                        "PYTHONPATH": os.pathsep.join(
                            filter(
                                None,
                                (
                                    str(Path(__file__).resolve().parent),
                                    os.environ.get("PYTHONPATH"),
                                ),
                            )
                        ),
                        **{
                            k: v
                            for k, v in os.environ.items()
                            if k
                            in {
                                "PATH",
                                "HOME",
                                "TMP",
                                "TMPDIR",
                                "TEMP",
                                "SYSTEMROOT",
                                "LANG",
                            }
                        },
                    },
                )
                self.assertEqual(process.returncode, 91, process.stderr.decode())
                saved = json.loads(packet.read_text())
                f.root = child_root
                f.policy = f.policy_ref = saved["policy"]
                f.comment = saved["comment"]
                f.extra_comments = {int(k): v for k, v in saved["extra"].items()}
                f.provider_calls = saved["provider_calls"]
                old = parse_session_comment(f.comment["body"], identity=IDENTITY)
                self.assertEqual(old.transaction.phase, "publication_pending")
                predecessor = ApiRunAccounting.from_document(old.api_run_accounting)
                f.attempt = 2
                f.previous_active = active
                self.assertEqual(f.invoke(), 1 if active else 0, f.error)
                self.assertEqual(f.provider_calls, 1)
                if active:
                    self.assertEqual(
                        parse_session_comment(
                            f.comment["body"], identity=IDENTITY
                        ).to_dict(),
                        old.to_dict(),
                    )
                    self.assertFalse(
                        any(
                            m in {"POST", "PATCH"} and "/issues" in p
                            for _, m, p in f.trace
                        )
                    )
                else:
                    self.assertEqual(
                        f.accounted.accounting.blocks[: len(predecessor.blocks)],
                        predecessor.blocks,
                    )
                    self.assertEqual(
                        sum(r["state"] == "APPROVED" for r in f.reviews), 1
                    )

    def test_stopped_same_run_successor_publishes_retained_result_without_inference(
        self,
    ):
        from review_sensei_github.session_ledger import parse_session_comment

        with tempfile.TemporaryDirectory() as directory:
            f = EntryFixture(directory, limit=512)

            def interrupted():
                raise KeyboardInterrupt()

            f.before_publication = interrupted
            self.assertEqual(f.invoke(), 130, f.error)
            pending = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertEqual(pending.transaction.phase, "publication_pending")
            f.attempt = 2
            f.before_publication = None
            self.assertEqual(f.invoke(), 0, f.error)
            self.assertEqual(f.provider_calls, 1)
            final = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertEqual(
                final.transaction.transaction_id, pending.transaction.transaction_id
            )
            self.assertEqual(final.transaction.phase, "publication_succeeded")
            self.assertEqual(sum(r["state"] == "APPROVED" for r in f.reviews), 1)

    def test_actual_package_entry_owns_analysis_and_publication(self):
        import json

        from test_api_work_lifecycle import caller_measurement

        for human in (False, True):
            with self.subTest(human=human), tempfile.TemporaryDirectory() as directory:
                f = EntryFixture(directory)
                self.assertEqual(f.invoke(human=human), 0, f.error)
                self.assertEqual(len(f.transports), 1)
                self.assertEqual(f.accounted.accounting.configuration.commit, f.policy)
                self.assertNotEqual(f.policy, BASE)
                self.assertEqual(
                    any(r["state"] == "APPROVED" for r in f.reviews), not human
                )
                self.assertEqual(
                    json.loads((f.root / "publication.json").read_text())["status"],
                    "published",
                )
                self.assertEqual(f.accounted._terminal_reason, "publication-completion")
                self.assertEqual(f.provider_calls, 1)
                self.assertEqual(len(f.trace), 84 if human else 88)
                self.assertEqual(caller_measurement(f)["startup_reservation"], 130)

    def test_operation_app_identity_comes_from_worker_and_cannot_fall_back(self):
        import os

        for missing_broker_identity in (False, True):
            with (
                self.subTest(missing=missing_broker_identity),
                tempfile.TemporaryDirectory() as directory,
            ):
                fixture = EntryFixture(directory)
                if missing_broker_identity:
                    original = fixture.broker_response

                    def without_app_id(payload):
                        response = original(payload)
                        response.pop("app_id", None)
                        return response

                    fixture.broker_response = without_app_id
                else:
                    fixture.before_entry = lambda: os.environ.pop(
                        "REVIEWSENSEI_APP_ID", None
                    )
                self.assertEqual(
                    fixture.invoke(), 1 if missing_broker_identity else 0, fixture.error
                )
                self.assertEqual(
                    fixture.provider_calls, 0 if missing_broker_identity else 1
                )
                if missing_broker_identity:
                    self.assertIsNone(fixture.comment)
                    self.assertIn("authenticated App identity", fixture.error)
                else:
                    from review_sensei_github.session_ledger import (
                        parse_session_comment,
                    )

                    stored = parse_session_comment(
                        fixture.comment["body"], identity=IDENTITY
                    )
                    self.assertEqual(
                        fixture.accounted.accounting.terminal_transaction,
                        stored.transaction.transaction_id,
                    )

    def test_diagnostic_files_cannot_replace_in_memory_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            f = EntryFixture(directory)

            def corrupt():
                for name in ("review.json", "configuration.json", "admission.json"):
                    (f.root / name).write_text('{"untrusted":"not authority"}')

            f.after_engine = corrupt
            self.assertEqual(f.invoke(), 0, f.error)
            self.assertTrue(any(r["state"] == "APPROVED" for r in f.reviews))

    def test_policy_head_and_overlap_changes_refuse_before_provider(self):
        for change in ("policy", "head", "overlap"):
            with (
                self.subTest(change=change),
                tempfile.TemporaryDirectory() as directory,
            ):
                f = EntryFixture(directory)
                if change == "policy":
                    f.policy_ref = "c" * 40
                if change == "head":
                    f.live_head = "d" * 40
                if change == "overlap":
                    f.overlap = True
                self.assertEqual(f.invoke(), 1)
                self.assertEqual(f.provider_calls, 0)
                self.assertIsNone(f.comment)
                self.assertFalse(
                    any(
                        m in {"POST", "PATCH"} and "/issues" in p for _, m, p in f.trace
                    )
                )

    def test_duplicate_startup_cannot_open_another_process_allowance(self):
        with tempfile.TemporaryDirectory() as directory:
            f = EntryFixture(directory)
            self.assertEqual(f.invoke(), 0, f.error)
            before = len(f.trace)
            state = f.accounted.accounting
            self.assertEqual(f.invoke(), 1)
            self.assertEqual(len(f.trace), before)
            self.assertEqual(f.accounted.accounting, state)

    def test_provider_failure_and_handled_cancel_have_one_cleanup_owner(self):
        from review_sensei.errors import ReviewInputError

        for error, expected in (
            (ReviewInputError("synthetic provider failure"), 1),
            (KeyboardInterrupt(), 130),
        ):
            with (
                self.subTest(kind=type(error).__name__),
                tempfile.TemporaryDirectory() as directory,
            ):
                f = EntryFixture(directory)

                def fail():
                    raise error

                f.before_provider = fail
                self.assertEqual(f.invoke(), expected, f.error)
                self.assertEqual(f.accounted._terminal_reason, "analysis-abort")
                self.assertFalse(f.reviews)

    def test_cancellation_before_publication_retains_pending_work(self):
        from review_sensei_github.session_ledger import parse_session_comment

        with tempfile.TemporaryDirectory() as directory:
            f = EntryFixture(directory)

            def cancel():
                raise KeyboardInterrupt()

            f.before_publication = cancel
            self.assertEqual(f.invoke(), 130, f.error)
            self.assertFalse(f.reviews)
            self.assertEqual(
                parse_session_comment(
                    f.comment["body"], identity=IDENTITY
                ).transaction.phase,
                "publication_pending",
            )

    def test_unknown_initial_post_is_confirmed_exactly_without_repost(self):
        for retained in (True, False):
            with (
                self.subTest(retained=retained),
                tempfile.TemporaryDirectory() as directory,
            ):
                f = EntryFixture(directory)
                f.fault = "unknown-stored" if retained else "unknown-absent"
                self.assertEqual(f.invoke(), 0 if retained else 1, f.error)
                roots = [
                    m
                    for _, m, p in f.trace
                    if m == "POST" and p.endswith("/issues/7/comments")
                ]
                # Extra immutable result parts use POST too; distinguish the
                # unique session creation through the recorded native writes.
                self.assertEqual(
                    sum(
                        mode[0] == 1 and mode[1] == "startup" for mode in f.write_modes
                    ),
                    1,
                )
                self.assertGreaterEqual(len(roots), 1)
                self.assertEqual(
                    sum(o["outcome"] == "unknown" for o in f.accounted.observations), 1
                )
                if not retained:
                    self.assertEqual(f.provider_calls, 0)

    def test_unknown_checkpoint_patch_uses_original_funded_readback(self):
        with tempfile.TemporaryDirectory() as directory:
            f = EntryFixture(directory)

            def lose():
                f.fault = "unknown-stored"

            f.before_provider = lose
            self.assertEqual(f.invoke(), 0, f.error)
            self.assertEqual(f.accounted.accounting.uncertain, 1)
            self.assertIsNone(f.accounted._checkpoint)
            self.assertTrue(any(r["state"] == "APPROVED" for r in f.reviews))

    def test_rejected_worker_post_still_costs_one_caller_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            f = EntryFixture(directory)
            f.refuse_broker = True
            self.assertEqual(f.invoke(), 1)
            self.assertEqual(
                len(f.trace), 6
            )  # Four policy/PR reads, OIDC, rejected Worker POST
            self.assertEqual(f.accounted.startup_dispatches, 6)
            self.assertEqual(f.provider_calls, 0)

    def test_local_review_uses_same_owner_and_meters_credential_free_model_discovery(
        self,
    ):
        # The fixed fixture adds one caller discovery request per owner; this
        # is a test-derived input, not a shipping default or a new phase pool.
        with tempfile.TemporaryDirectory() as directory:
            f = EntryFixture(
                directory, runner_kind="local", limit=QUALIFICATION_LIMIT + 2
            )
            self.assertEqual(f.invoke(), 0, f.error)
            self.assertEqual(f.accounted.accounting.owner.job, "local")
            self.assertEqual(len(f.transports), 1)
            self.assertEqual(len(f.trace), 89)
            self.assertEqual(sum(p == "/api/tags" for _, _, p in f.trace), 1)
            self.assertTrue(any(r["state"] == "APPROVED" for r in f.reviews))

    def test_unfunded_publication_stops_without_new_allowance(self):
        with tempfile.TemporaryDirectory() as directory:
            f = EntryFixture(directory, limit=QUALIFICATION_LIMIT - 4)
            self.assertEqual(f.invoke(), 1)
            self.assertEqual(len(f.transports), 1)
            self.assertFalse(any(r["state"] == "APPROVED" for r in f.reviews))
            self.assertEqual(
                f.accounted.accounting.configuration.limit, QUALIFICATION_LIMIT - 4
            )
            self.assertEqual(f.accounted._terminal_reason, "publication-completion")

    def test_frozen_configuration_cannot_change_while_provider_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            f = EntryFixture(directory)

            def change():
                path = f.root / ".reviewsensei.yml"
                path.write_text(
                    path.read_text().replace(
                        "api_request_limit: 512", "api_request_limit: 514"
                    )
                )

            f.before_provider = change
            self.assertEqual(f.invoke(), 1)
            self.assertFalse(f.reviews)
            self.assertEqual(f.accounted.accounting.configuration.limit, 512)

    def test_changed_head_before_publication_preserves_pending_review(self):
        from review_sensei_github.session_ledger import parse_session_comment

        with tempfile.TemporaryDirectory() as directory:
            f = EntryFixture(directory)

            def move():
                f.live_head = "d" * 40

            f.before_publication = move
            self.assertEqual(f.invoke(), 1)
            self.assertFalse(f.reviews)
            self.assertEqual(
                parse_session_comment(
                    f.comment["body"], identity=IDENTITY
                ).transaction.phase,
                "publication_pending",
            )

    def test_undersized_configuration_stops_before_business_http(self):
        with tempfile.TemporaryDirectory() as directory:
            f = EntryFixture(directory, limit=128)
            self.assertEqual(f.invoke(), 1)
            self.assertFalse(f.trace)
            self.assertIsNone(f.comment)
            self.assertEqual(f.provider_calls, 0)


class ConsecutiveReviewRunTests(unittest.TestCase):
    def test_historical_fixture_keeps_canonical_diff_bytes_with_crlf_defaults(self):
        from unittest.mock import patch

        write_text = Path.write_text

        def platform_write(path, data, encoding=None, errors=None, newline=None):
            if path.name == "review.patch" and newline is None:
                newline = "\r\n"
            return write_text(
                path, data, encoding=encoding, errors=errors, newline=newline
            )

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(Path, "write_text", platform_write),
        ):
            f = self.historical_case(directory)
            self.assertNotIn(b"\r", (f.root / "review.patch").read_bytes())
            self.assertEqual(f.invoke(human=True), 0, f.error)

    def test_historical_receipts_are_evidence_and_cannot_restore_enrollment_authority(
        self,
    ):
        import copy
        import sys

        from review_sensei_github.action_completion import CompletionState
        from review_sensei_github.api_run_history import read_previous_run
        from review_sensei_github.historical_completion import (
            HistoricalPublicationCompletion,
        )

        from review_sensei.errors import ReviewInputError

        with tempfile.TemporaryDirectory() as directory:
            f = self.historical_case(directory)
            issued = []

            def observe(frame, event, value):
                if (
                    event != "return"
                    or frame.f_code.co_name != "authenticate_historical_publication"
                ):
                    return
                self.assertIsInstance(value, HistoricalPublicationCompletion)
                issued.append(value)
                selection = value.selection
                args = dict(
                    caller=selection.caller,
                    ledger=selection.ledger,
                    identity=selection.identity,
                    configuration=selection.configuration,
                    owner=selection.owner,
                    previous=selection.record,
                )
                value.require(**args)
                for forged in (
                    replace(value),
                    HistoricalPublicationCompletion(
                        selection, value.receipt_bytes, CompletionState.CONFIRMED
                    ),
                ):
                    with self.assertRaisesRegex(
                        ReviewInputError, "restored or foreign"
                    ):
                        forged.require(**args)
                for change in (
                    {"caller": object()},
                    {
                        "configuration": replace(
                            selection.configuration, digest="0" * 64
                        )
                    },
                    {
                        "previous": selection.record.evolve(
                            generation=selection.record.generation + 1
                        )
                    },
                ):
                    with self.assertRaisesRegex(
                        ReviewInputError, "stale, restored or foreign"
                    ):
                        value.require(**{**args, **change})

            sys.setprofile(observe)
            try:
                self.assertEqual(f.invoke(human=True), 0, f.error)
            finally:
                sys.setprofile(None)
            self.assertEqual(len(issued), 1)
            proof = issued[0]
            selection = proof.selection
            with self.assertRaisesRegex(
                ReviewInputError, "lacks current startup authority"
            ):
                proof.require(
                    caller=f.accounted,
                    ledger=selection.ledger,
                    identity=IDENTITY,
                    configuration=selection.configuration,
                    owner=selection.owner,
                    previous=selection.record,
                )
            for key, value in (
                ("current_head_sha", "0" * 40),
                ("historical_accounting_sha256", "0" * 64),
                ("transaction_id", "0" * 64),
            ):
                reference = copy.deepcopy(f.accounted.accounting.previous_record)
                reference["completion"][key] = value
                with self.subTest(key=key), self.assertRaises(ReviewInputError):
                    read_previous_run(f.selected.ledger, IDENTITY, reference)

    def test_historical_transition_process_death_never_infers_nonentry_from_absence(
        self,
    ):
        import json
        import os
        import subprocess
        import sys

        from review_sensei_github.session_ledger import parse_session_comment

        from review_sensei.api_work import ApiRunAccounting

        script = r"""
import json, os, sys
from pathlib import Path
from test_review_run import ConsecutiveReviewRunTests
f = ConsecutiveReviewRunTests.historical_case(sys.argv[1])
stage, packet = sys.argv[2], Path(sys.argv[3])
def killed():
    packet.write_text(json.dumps({
        "comment": f.comment, "extra": f.extra_comments, "reviews": f.reviews,
        "inline": f.inline, "checks": f.check_runs, "threads": f.threads,
        "policy": f.policy, "provider_calls": f.provider_calls, "run_id": f.run_id,
        "accounting": f.accounted.accounting.to_document(),
        "run_results": [[r, a, v] for (r, a), v in f.run_results.items()],
        "run_heads": [[r, a, v] for (r, a), v in f.run_heads.items()],
        "activation_body": getattr(f, "activation_body", None),
        "delayed_body": getattr(f, "delayed_body", None),
    }))
    os._exit(91)
def observed(frame, event, value):
    if event != "return":
        return
    name, filename = frame.f_code.co_name, frame.f_code.co_filename
    if name == "_accept_activation" and f.accounted._state is not None:
        f.activation_body = f.comment["body"]
        if stage == "enrolled":
            killed()
    if stage == "reservation" and name == "prepare_session_round" and value.reservation_id is not None:
        killed()
    if stage == "attached" and name == "prepare_review_transaction" and value.transaction is not None:
        killed()
    if stage == "provider-entry" and name == "begin_provider_phase" and filename.endswith("api_run_host.py"):
        killed()
    if stage == "provider-return" and name == "complete" and f.provider_calls == 3:
        killed()
original_external = f.external
def external(request, timeout):
    if stage == "reservation-request" and request.method == "PATCH":
        from review_sensei_github.session_ledger import parse_session_comment
        from test_review_run import IDENTITY
        payload = json.loads(request.data)
        pending = parse_session_comment(payload.get("body", ""), identity=IDENTITY)
        if pending is not None and pending.reservation_id is not None:
            f.delayed_body = payload["body"]
            killed()
    return original_external(request, timeout)
f.external = external
sys.setprofile(observed)
f.invoke(human=True)
os._exit(92)
"""
        for stage in (
            "enrolled",
            "reservation-request",
            "reservation",
            "attached",
            "provider-entry",
            "provider-return",
        ):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                child = root / "child"
                child.mkdir()
                packet = root / "killed.json"
                environment = fixture_process_environment()
                environment["PYTHONPATH"] = os.pathsep.join(
                    filter(
                        None,
                        (
                            str(Path(__file__).resolve().parent),
                            os.environ.get("PYTHONPATH"),
                        ),
                    )
                )
                process = subprocess.run(
                    [sys.executable, "-c", script, str(child), stage, str(packet)],
                    capture_output=True,
                    timeout=30,
                    env=environment,
                )
                self.assertEqual(process.returncode, 91, process.stderr.decode())
                saved = json.loads(packet.read_text())
                if stage == "reservation-request":
                    self.assertEqual(saved["comment"]["body"], saved["activation_body"])
                    self.assertIsNotNone(saved["delayed_body"])
                (root / "parent").mkdir()
                f = _CompletionFixture(str(root / "parent"))
                f.root = child
                f.policy = f.policy_ref = saved["policy"]
                f.comment = saved["comment"]
                f.extra_comments = {int(k): v for k, v in saved["extra"].items()}
                f.reviews, f.inline, f.check_runs, f.threads = (
                    saved[k] for k in ("reviews", "inline", "checks", "threads")
                )
                f.provider_calls, f.run_id = saved["provider_calls"], saved["run_id"]
                f.run_results = {(r, a): v for r, a, v in saved["run_results"]}
                f.run_heads = {(r, a): v for r, a, v in saved["run_heads"]}
                f.run_results[f.run_id, 1] = 91
                f.live_base = f.requested_base = "f" * 40
                f.live_head = f.requested_head = "e" * 40
                f.attempt, f.mode = 2, "confirmed"
                before = parse_session_comment(f.comment["body"], identity=IDENTITY)
                prior = ApiRunAccounting.from_document(before.api_run_accounting)
                calls = f.provider_calls
                self.assertEqual(f.invoke(human=True), 1, f.error)
                self.assertEqual(f.provider_calls, calls)
                self.assertEqual(f.comment["body"], saved["comment"]["body"])
                self.assertEqual(
                    ApiRunAccounting.from_document(
                        parse_session_comment(
                            f.comment["body"], identity=IDENTITY
                        ).api_run_accounting
                    ),
                    prior,
                )
                self.assertIn(
                    "cannot be established"
                    if stage == "reservation"
                    else "no proven provider non-entry",
                    f.error,
                )
                if stage == "reservation-request":
                    # The external server may accept that old PATCH after the
                    # refusal. No successor work or ownership has been issued.
                    f.comment["body"] = saved["delayed_body"]
                    late = parse_session_comment(f.comment["body"], identity=IDENTITY)
                    self.assertIsNotNone(late.reservation_id)
                    self.assertEqual(
                        ApiRunAccounting.from_document(
                            late.api_run_accounting
                        ).owner.run_attempt,
                        1,
                    )
                    self.assertEqual(f.provider_calls, calls)

    @staticmethod
    def historical_case(
        directory, fixture_type=_CompletionFixture, *, partial_predecessor=False
    ):
        f = fixture_type(directory, mode="refused")
        patch = f.root / "review.patch"
        original = patch.read_text()
        patch.write_text(
            original
            + "\ndiff --git a/image.bin b/image.bin\nnew file mode 100644\nindex 0000000..1111111\nBinary files /dev/null and b/image.bin differ\n",
            encoding="utf-8",
            newline="\n",
        )
        if f.invoke(human=True) != 0:
            raise AssertionError(f.error)
        if not partial_predecessor:
            patch.write_text(original, encoding="utf-8", newline="\n")
        f.next_run()
        if f.invoke(human=True) != 0:
            raise AssertionError("original soft cleanup refusal was not exercised")
        patch.write_text(original, encoding="utf-8", newline="\n")
        if f.threads[0]["isResolved"]:
            raise AssertionError("original cleanup refusal did not retain target")
        # Synthetic external state after a separately authorized confirmation;
        # the historical verifier itself never performs this mutation.
        from review_sensei_github.publication import finding_publication_scope

        for thread in f.threads:
            scope = finding_publication_scope(thread["comments"]["nodes"][0]["body"])
            if scope is not None and scope[2] != f.requested_head:
                thread["isResolved"] = True
        f.next_run()
        f.mode = "confirmed"
        f.live_head = f.requested_head = "e" * 40
        f.live_base = f.requested_base = "f" * 40
        return f

    def test_historical_completion_enrolls_fresh_base_and_preserves_exact_predecessor(
        self,
    ):
        import copy
        import os
        import subprocess

        from review_sensei_github.api_run_history import read_previous_run
        from review_sensei_github.publication import parse_finding_index
        from review_sensei_github.session_ledger import parse_session_comment

        for fault in (None, "archive-stored", "transition-stored", "new-policy-1024"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                f = self.historical_case(directory)
                previous = parse_session_comment(f.comment["body"], identity=IDENTITY)
                old_document = copy.deepcopy(previous.to_dict())
                old_provider = f.provider_calls
                if fault == "new-policy-1024":
                    f.config_bytes = f.config_bytes.replace(
                        b"api_request_limit: 512", b"api_request_limit: 1024"
                    )
                    (f.root / ".reviewsensei.yml").write_bytes(f.config_bytes)
                    subprocess.run(
                        [
                            "git",
                            "-C",
                            str(f.root),
                            "-c",
                            "user.name=Fixture",
                            "-c",
                            "user.email=fixture@example.invalid",
                            "-c",
                            "user.useConfigOnly=true",
                            "commit",
                            "-am",
                            "new current policy",
                        ],
                        check=True,
                        capture_output=True,
                        env=dict(
                            fixture_process_environment(),
                            GIT_CONFIG_GLOBAL=os.devnull,
                            GIT_CONFIG_NOSYSTEM="1",
                        ),
                    )
                    f.policy_ref = f.policy = subprocess.check_output(
                        ["git", "-C", str(f.root), "rev-parse", "HEAD"], text=True
                    ).strip()
                else:
                    f.rollover_fault = fault
                seen = []

                def before_provider():
                    self.assertEqual(f.provider_calls, old_provider)
                    state = f.accounted.accounting
                    self.assertEqual(state.identity.run_id, f.run_id)
                    restored = read_previous_run(
                        f.selected.ledger, IDENTITY, state.previous_record
                    )
                    self.assertEqual(restored.to_dict(), old_document)
                    self.assertIsNone(
                        restored.api_run_accounting["terminal_transaction"]
                    )
                    receipt = state.previous_record["completion"]
                    self.assertEqual(
                        receipt["historical_base_sha"], previous.transaction.base_sha
                    )
                    self.assertEqual(
                        receipt["historical_head_sha"], previous.transaction.head_sha
                    )
                    self.assertEqual(receipt["current_base_sha"], f.requested_base)
                    self.assertEqual(receipt["current_head_sha"], f.requested_head)
                    seen.append(True)

                f.before_entry = before_provider
                self.assertEqual(f.invoke(human=True), 0, f.error)
                self.assertEqual(seen, [True])
                self.assertEqual(f.provider_calls, old_provider + 1)
                self.assertLessEqual(f.accounted.startup_dispatches, 77)
                self.assertEqual(
                    f.accounted.accounting.configuration.limit,
                    1024 if fault == "new-policy-1024" else 512,
                )
                current = parse_session_comment(f.comment["body"], identity=IDENTITY)
                self.assertEqual(
                    (current.transaction.base_sha, current.transaction.head_sha),
                    (f.requested_base, f.requested_head),
                )
                self.assertNotEqual(
                    current.transaction.transaction_id,
                    previous.transaction.transaction_id,
                )
                self.assertIsNotNone(current.api_run_accounting["terminal_transaction"])
                self.assertEqual(f.check_runs[-1]["conclusion"], "action_required")
                index = parse_finding_index(
                    f.reviews[-1]["body"],
                    repository_id=123,
                    pull_request=7,
                    head_sha=f.requested_head,
                )
                self.assertTrue(any(entry.needs_human for entry in index.entries))
                self.assertEqual(previous.to_dict(), old_document)
                self.assertEqual(
                    sum(
                        method == "POST"
                        and path.endswith("/issues/7/comments")
                        and observation["phase"] == "startup"
                        for observation, (_, method, path) in zip(
                            f.accounted.observations, f.trace
                        )
                    ),
                    1,
                )
                if fault is None:
                    # Ordinary following epochs keep the v2 evidence chain;
                    # they do not reinterpret its receipt as new authority.
                    f.before_entry = None
                    f.next_run()
                    f.live_head = f.requested_head = "1" * 40
                    self.assertEqual(f.invoke(human=True), 0, f.error)
                    archived_current = read_previous_run(
                        f.selected.ledger,
                        IDENTITY,
                        f.accounted.accounting.previous_record,
                    )
                    older = read_previous_run(
                        f.selected.ledger,
                        IDENTITY,
                        archived_current.api_run_accounting["previous_record"],
                    )
                    self.assertEqual(older.to_dict(), old_document)

    def test_historical_transition_refuses_missing_effects_and_unfunded_tail_before_provider(
        self,
    ):
        import copy

        from review_sensei_github.session_ledger import parse_session_comment

        for fault, diagnostic in (
            ("unresolved", "superseded cleanup target remains unresolved"),
            ("unchanged-revision", "outside historical completion scope"),
            ("base-only", "outside historical completion scope"),
            ("missing-index", "historical finding inventory is unverifiable"),
            ("edited", "edited after publication"),
            ("missing-root", "original cleanup target is missing"),
            ("check", "required check does not confirm"),
            ("closing-change", "cleanup inventory changed"),
            ("overlap", "overlapping"),
            ("previous-active", "successful review exit"),
            ("failed-owner", "successful review exit"),
            ("cancelled-owner", "successful review exit"),
            ("unknown-source", "source is unrecognized"),
            ("masked-step-failure", "successful exit"),
            ("capacity", "cannot fund the complete operation"),
            ("archive-absent", "retained run create remains uncertain"),
            ("transition-absent", "unconfirmed"),
            ("foreign-archive", "owner"),
            ("base-after-archive", "fresh current PR authority changed"),
            ("root-after-archive", "generation conflict"),
        ):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                f = self.historical_case(directory)
                before = copy.deepcopy(f.comment)
                provider = f.provider_calls
                f.completion_fault = fault
                if fault == "unresolved":
                    f.threads[0]["isResolved"] = False
                elif fault == "base-only":
                    original = parse_session_comment(
                        before["body"], identity=IDENTITY
                    ).transaction
                    f.live_head = f.requested_head = original.head_sha
                elif fault == "unchanged-revision":
                    original = parse_session_comment(
                        before["body"], identity=IDENTITY
                    ).transaction
                    f.live_base = f.requested_base = original.base_sha
                    f.live_head = f.requested_head = original.head_sha
                elif fault == "missing-index":
                    from review_sensei_github.publication import (
                        FINDING_INDEX_MARKER_PREFIX,
                    )

                    body = f.reviews[0]["body"]
                    start = body.index(FINDING_INDEX_MARKER_PREFIX)
                    end = body.index("-->", start) + 3
                    f.reviews[0]["body"] = body[:start] + body[end:]
                elif fault == "overlap":
                    f.overlap = True
                elif fault == "previous-active":
                    f.previous_active = True
                elif fault == "capacity":
                    for i in range(200):
                        f.extra_comments[9_000_000 + i] = {
                            "id": 9_000_000 + i,
                            "body": "unrelated history",
                            "user": {"id": 888, "login": "alice", "type": "User"},
                            "issue_url": "https://api.github.test/repos/owner/repo/issues/7",
                        }
                elif fault in {"archive-absent", "transition-absent"}:
                    f.rollover_fault = fault
                elif fault == "foreign-archive":
                    f.archive_bad = "owner"
                self.assertEqual(f.invoke(human=True), 1, f.error)
                self.assertIn(diagnostic, f.error)
                self.assertEqual(f.provider_calls, provider)
                self.assertIsNone(f.accounted._state)
                if fault != "root-after-archive":
                    self.assertEqual(f.comment, before)
                if fault not in {
                    "archive-absent",
                    "transition-absent",
                    "foreign-archive",
                    "base-after-archive",
                    "root-after-archive",
                }:
                    self.assertFalse(
                        any(
                            method in {"POST", "PATCH"} and "/issues" in path
                            for _, method, path in f.trace
                        )
                    )
                root = parse_session_comment(f.comment["body"], identity=IDENTITY)
                self.assertIsNone(root.api_run_accounting["terminal_transaction"])

    def test_historical_hold_root_capacity_refuses_before_any_archive_write(self):
        import hashlib

        from review_sensei_github.session_ledger import parse_session_comment

        from review_sensei.errors import ReviewInputError

        with tempfile.TemporaryDirectory() as directory:
            f = self.historical_case(directory)
            previous = parse_session_comment(f.comment["body"], identity=IDENTITY)
            largest = None
            for count in range(1, 100):
                rows = [
                    {
                        "fingerprint": hashlib.sha256(str(i).encode()).hexdigest(),
                        "action": "defer",
                        "reason": "x" * 480,
                        "actor": "alice",
                        "head_sha": "a" * 40,
                    }
                    for i in range(count)
                ]
                try:
                    value = previous.evolve(
                        generation=previous.generation + 1, dispositions=rows
                    )
                except ReviewInputError:
                    break
                largest = value
            self.assertIsNotNone(largest)
            import copy

            history = copy.deepcopy(previous.convergence_history)
            from dataclasses import replace

            from review_sensei.baseline import (
                baseline_from_history_document,
                baseline_history_document,
            )

            baseline = baseline_from_history_document(history["baseline"])
            for count in range(1, 256):
                findings = tuple(
                    replace(
                        baseline.findings[0],
                        fingerprint=hashlib.sha256(
                            ("fp" + str(i)).encode()
                        ).hexdigest(),
                        resolution_criterion=hashlib.sha256(
                            ("criterion" + str(i)).encode()
                        ).hexdigest(),
                        concern=hashlib.sha256(
                            ("concern" + str(i)).encode()
                        ).hexdigest(),
                    )
                    for i in range(count)
                )
                try:
                    document = baseline_history_document(
                        replace(baseline, findings=findings)
                    )
                except ReviewInputError:
                    break
                history["baseline"] = document
            for count in range(1, 65):
                history["progress"] = [
                    {
                        "event": "completed",
                        "generation": 0,
                        "blocker_set_sha256": hashlib.sha256(
                            str(i).encode()
                        ).hexdigest(),
                        "blocker_count": 1,
                        "transaction_id": hashlib.sha256(
                            ("tx" + str(i)).encode()
                        ).hexdigest(),
                    }
                    for i in range(count)
                ]
                try:
                    value = largest.evolve(convergence_history=history)
                except ReviewInputError:
                    break
                fullest = value
            largest = fullest
            # Obsolete grants are retained inert history, never authority.
            grants = [
                {
                    "command_id": str(i) + "c" * 120,
                    "actor": "a" * 256,
                    "head_sha": "a" * 40,
                    "policy_digest": "b" * 64,
                    "issued_at": "2026-01-01T00:00:00Z",
                    "expires_at": "2026-01-02T00:00:00Z",
                    "consumed_reservation_id": None,
                    "consumed_generation": None,
                }
                for i in range(4)
            ]
            try:
                largest = largest.evolve(continuation_grants=grants)
            except ReviewInputError:
                pass
            f.rewrite_record(mutate=lambda _record: largest)
            before = f.comment["body"]
            calls = f.provider_calls
            self.assertEqual(f.invoke(human=True), 1, f.error)
            self.assertIn("size limit", f.error)
            self.assertEqual(f.comment["body"], before)
            self.assertEqual(f.provider_calls, calls)
            self.assertIsNone(f.accounted._state)
            self.assertFalse(
                any(
                    method in {"POST", "PATCH"} and "/issues" in path
                    for _, method, path in f.trace
                )
            )

    def test_historical_transition_refuses_uncertain_original_terminal_write(self):
        import copy

        from review_sensei_github.session_ledger import parse_session_comment

        with tempfile.TemporaryDirectory() as directory:
            f = _CompletionFixture(directory, mode="confirmed")
            self.assertEqual(f.invoke(human=True), 0, f.error)
            f.next_run()
            f.terminal_write_fault = True
            self.assertEqual(f.invoke(human=True), 1, f.error)
            before = copy.deepcopy(f.comment)
            previous = parse_session_comment(before["body"], identity=IDENTITY)
            self.assertEqual(previous.transaction.phase, "publication_succeeded")
            self.assertIsNone(previous.api_run_accounting["terminal_transaction"])
            provider = f.provider_calls
            f.next_run()
            f.terminal_write_fault = False
            f.live_head = f.requested_head = "e" * 40
            f.live_base = f.requested_base = "f" * 40
            self.assertEqual(f.invoke(human=True), 1, f.error)
            self.assertIn("successful review exit", f.error)
            self.assertEqual(f.provider_calls, provider)
            self.assertEqual(f.comment, before)
            self.assertIsNone(f.accounted._state)

    def test_completion_authentication_cannot_mutate_host_effects(self):
        from review_sensei_github.http import GitHubHttp
        from review_sensei_github.review_completion_recovery import _ReadOnlyHttp

        from review_sensei.errors import ReviewInputError

        def never_enter(request, timeout):
            self.fail("a prohibited completion mutation reached external transport")

        http = _ReadOnlyHttp(GitHubHttp(opener=never_enter))
        for method, path, body in (
            ("PATCH", "/repos/owner/repo/check-runs/1", {}),
            ("POST", "/repos/owner/repo/pulls/7/reviews", {}),
            (
                "POST",
                "/graphql",
                {
                    "operationName": "ReviewCompletionThreads",
                    "query": "mutation { resolveReviewThread }",
                },
            ),
            (
                "POST",
                "/graphql",
                {"operationName": "ResolveReviewThread", "query": "query { node }"},
            ),
        ):
            with (
                self.subTest(method=method, path=path),
                self.assertRaisesRegex(ReviewInputError, "read-only"),
            ):
                http.request(method, path, token="synthetic", body=body)

    def test_successor_cannot_infer_tail_completion_from_published_transaction(self):
        from review_sensei_github.session_ledger import parse_session_comment

        from review_sensei.api_work import ApiRunAccounting

        for mode in ("confirmed", "refused", "lost-ack", "lost-unstored"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                f = _CleanupTailFixture(directory, mode)
                patch = f.root / "review.patch"
                patch.write_text(
                    patch.read_text()
                    + "\ndiff --git a/image.bin b/image.bin\nnew file mode 100644\n"
                    "index 0000000..1111111\n"
                    "Binary files /dev/null and b/image.bin differ\n",
                    encoding="utf-8",
                    newline="\n",
                )
                self.assertEqual(f.invoke(human=True), 0, f.error)
                initial = parse_session_comment(f.comment["body"], identity=IDENTITY)
                self.assertIsNotNone(initial.api_run_accounting["terminal_transaction"])
                f.next_run()
                self.assertEqual(
                    f.invoke(human=True), 1 if mode == "lost-ack" else 0, f.error
                )
                before = parse_session_comment(f.comment["body"], identity=IDENTITY)
                self.assertEqual(before.transaction.phase, "publication_succeeded")
                if mode == "confirmed":
                    self.assertIsNotNone(
                        before.api_run_accounting["terminal_transaction"]
                    )
                    continue
                self.assertIsNone(before.api_run_accounting["terminal_transaction"])
                provider_calls, reviews = f.provider_calls, len(f.reviews)
                mutations, queries = list(f.mutations), list(f.graphql_operations)
                prior_accounting = ApiRunAccounting.from_document(
                    before.api_run_accounting
                )
                f.mode = "confirmed"
                f.attempt = 2
                self.assertEqual(f.invoke(human=True), 1, f.error)
                self.assertIn("successor publication tail is unverified", f.error)
                after = parse_session_comment(f.comment["body"], identity=IDENTITY)
                self.assertIsNone(after.api_run_accounting["terminal_transaction"])
                self.assertEqual(after.transaction, before.transaction)
                self.assertEqual(after.dispositions, before.dispositions)
                self.assertEqual(f.provider_calls, provider_calls)
                self.assertEqual(len(f.reviews), reviews)
                self.assertEqual(f.mutations, mutations)
                self.assertEqual(f.graphql_operations, queries)
                current = ApiRunAccounting.from_document(after.api_run_accounting)
                self.assertEqual(current.identity, prior_accounting.identity)
                for block in prior_accounting.blocks:
                    self.assertIn(block, current.blocks)

    def test_second_ordinary_run_preserves_terminal_previous_root(self):
        import copy

        from review_sensei_github.session_ledger import parse_session_comment

        for human in (False, True):
            with self.subTest(human=human), tempfile.TemporaryDirectory() as directory:
                f = EntryFixture(directory)
                self.assertEqual(f.invoke(human=human), 0, f.error)
                previous = parse_session_comment(f.comment["body"], identity=IDENTITY)
                previous_bytes = copy.deepcopy(previous.to_dict())
                f.next_run()
                # The new-head provider emits no findings. Existing human
                # obligations still withhold through normal convergence rules.
                self.assertEqual(f.invoke(human=False), 0, f.error)
                current = parse_session_comment(f.comment["body"], identity=IDENTITY)
                self.assertEqual(current.api_run_accounting["identity"]["run_id"], 457)
                self.assertEqual(
                    current.api_run_accounting["previous_record"]["root_sha256"],
                    previous.record_sha256,
                )
                self.assertEqual(previous.to_dict(), previous_bytes)
                self.assertEqual(f.provider_calls, 2)
                self.assertEqual(len(f.transports), 1)
                self.assertEqual(
                    sum(r["state"] == "APPROVED" for r in f.reviews), 0 if human else 2
                )
                self.assertIsNotNone(f.accounted.accounting.terminal_transaction)

    def test_active_incomplete_changed_or_nonterminal_predecessor_refuses(self):
        import copy

        from review_sensei.session import SessionRecord

        for mode in ("active", "incomplete", "changed-root", "nonterminal", "unclosed"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                f = EntryFixture(directory)
                self.assertEqual(f.invoke(), 0, f.error)
                if mode in {"nonterminal", "unclosed"}:

                    def unfinish(record):
                        data = record.to_dict()
                        data["api_run_accounting"]["terminal_transaction"] = None
                        if mode == "nonterminal":
                            data["transaction"]["phase"] = "publication_pending"
                        data.pop("record_sha256")
                        data.pop("schema_version")
                        data["transaction"] = replace(
                            record.transaction, phase=data["transaction"]["phase"]
                        )
                        return SessionRecord._construct(**data)

                    f.rewrite_record(mutate=unfinish)
                if mode == "active":
                    f.previous_active = True
                if mode == "incomplete":
                    f.previous_incomplete = True
                if mode == "changed-root":
                    f.change_root = True
                f.next_run()
                before = copy.deepcopy(f.comment)
                self.assertEqual(f.invoke(), 1)
                self.assertEqual(f.provider_calls, 1)
                if mode != "changed-root":
                    self.assertEqual(f.comment, before)
                self.assertFalse(any(m == "PATCH" for _, m, _ in f.trace))
                if mode != "changed-root":
                    self.assertFalse(
                        any(m == "POST" and "/issues" in p for _, m, p in f.trace)
                    )

    def test_unknown_archive_or_transition_readback_never_replays_write(self):
        from review_sensei_github.session_ledger import parse_session_comment

        for mode in (
            "archive-stored",
            "archive-absent",
            "transition-stored",
            "transition-absent",
        ):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                f = EntryFixture(directory)
                self.assertEqual(f.invoke(), 0, f.error)
                old = parse_session_comment(f.comment["body"], identity=IDENTITY)
                f.next_run()
                f.rollover_fault = mode
                self.assertEqual(
                    f.invoke(), 0 if mode.endswith("stored") else 1, f.error
                )
                self.assertEqual(
                    sum(o["outcome"] == "unknown" for o in f.accounted.observations), 1
                )
                current = parse_session_comment(f.comment["body"], identity=IDENTITY)
                if mode.endswith("absent"):
                    self.assertEqual(current.record_sha256, old.record_sha256)
                    self.assertEqual(f.provider_calls, 1)
                else:
                    self.assertIsNotNone(f.accounted.accounting.terminal_transaction)
                # Only one archive POST, even after its uncertain response.
                archive_posts = [
                    o
                    for o, (_, method, path) in zip(f.accounted.observations, f.trace)
                    if o["phase"] == "startup"
                    and method in {"POST", "PATCH"}
                    and "/issues" in path
                ]
                self.assertEqual(
                    len(archive_posts), 1 if mode == "archive-absent" else 2
                )
                before = len(f.trace)
                self.assertEqual(f.invoke(), 1)
                self.assertEqual(len(f.trace), before)

    def test_previous_reconciled_unknown_charge_is_retained_without_transfer(self):
        from review_sensei_github.api_run_history import read_previous_run
        from review_sensei_github.session_ledger import parse_session_comment

        with tempfile.TemporaryDirectory() as directory:
            f = EntryFixture(directory)
            f.before_provider = lambda: setattr(f, "fault", "unknown-stored")
            self.assertEqual(f.invoke(), 0, f.error)
            old = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertTrue(old.api_run_accounting["terminal_transaction"])
            self.assertEqual(
                sum(len(b["pending"]) for b in old.api_run_accounting["blocks"]), 1
            )
            f.before_provider = None
            f.next_run()
            self.assertEqual(f.invoke(), 0, f.error)
            previous = read_previous_run(
                f.selected.ledger, IDENTITY, f.accounted.accounting.previous_record
            )
            self.assertEqual(previous.to_dict(), old.to_dict())
            self.assertEqual(f.accounted.accounting.uncertain, 0)

    def test_hard_kill_before_transaction_attachment_keeps_predecessor_capacity(self):
        import json
        import os
        import subprocess
        import sys

        from review_sensei_github.session_ledger import parse_session_comment

        from review_sensei.api_work import ApiRunAccounting

        for active in (True, False):
            with (
                self.subTest(active=active),
                tempfile.TemporaryDirectory() as directory,
            ):
                f = EntryFixture(directory)
                packet = f.root / "fake-remote.json"

                child_directory = f.root / "child"
                child_directory.mkdir()
                script = """
import json, os, sys
from pathlib import Path
from unittest.mock import patch
from test_review_run import EntryFixture
from review_sensei_github import review_run
f = EntryFixture(sys.argv[1])
def killed(*args, **kwargs):
    Path(sys.argv[2]).write_text(json.dumps({"comment": f.comment, "extra": f.extra_comments,
        "trace": f.trace, "accounting": f.accounted.accounting.to_document()}))
    os._exit(91)
with patch.object(review_run.ReviewRun, "execute", killed):
    f.invoke()
os._exit(92)
"""
                process = subprocess.run(
                    [sys.executable, "-c", script, str(child_directory), str(packet)],
                    env={
                        "PYTHONPATH": os.pathsep.join(
                            filter(
                                None,
                                (
                                    str(Path(__file__).resolve().parent),
                                    os.environ.get("PYTHONPATH"),
                                ),
                            )
                        ),
                        **{
                            key: value
                            for key, value in os.environ.items()
                            if key
                            in {
                                "PATH",
                                "HOME",
                                "TMP",
                                "TMPDIR",
                                "TEMP",
                                "SYSTEMROOT",
                                "LANG",
                            }
                        },
                    },
                    capture_output=True,
                    timeout=20,
                )
                self.assertEqual(process.returncode, 91, process.stderr.decode())
                before = json.loads(packet.read_text())
                f.root = child_directory
                f.policy = f.policy_ref = before["accounting"]["configuration"][
                    "commit"
                ]
                f.comment = before["comment"]
                f.extra_comments = {int(k): v for k, v in before["extra"].items()}
                prior = parse_session_comment(f.comment["body"], identity=IDENTITY)
                self.assertIsNone(prior.transaction)
                old = ApiRunAccounting.from_document(before["accounting"])
                f.attempt = 2
                f.previous_active = active
                self.assertEqual(f.invoke(), 1 if active else 0, f.error)
                if active:
                    self.assertFalse(
                        any(
                            m in {"POST", "PATCH"} and "/issues" in p
                            for _, m, p in f.trace
                        )
                    )
                    self.assertEqual(f.provider_calls, 0)
                else:
                    state = f.accounted.accounting
                    for block in old.blocks:
                        self.assertIn(block, state.blocks)
                    self.assertEqual(state.identity, old.identity)
                    self.assertEqual(state.configuration, old.configuration)
                    self.assertEqual(state.startup, old.startup)
                    self.assertEqual(state.owner.run_attempt, 2)
                    self.assertEqual(f.provider_calls, 1)

    def test_same_head_followup_closes_without_new_analysis_or_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            f = EntryFixture(directory)
            self.assertEqual(f.invoke(), 0, f.error)
            f.next_run()
            f.requested_head = f.live_head = HEAD
            self.assertEqual(f.invoke(), 0, f.error)
            self.assertEqual(f.provider_calls, 1)
            self.assertEqual(sum(r["state"] == "APPROVED" for r in f.reviews), 1)
            self.assertIsNotNone(f.accounted.accounting.terminal_transaction)
            self.assertEqual(f.accounted._terminal_reason, "already-published")

    def test_published_unconfirmed_cleanup_reports_terminal_pending_and_refuses_next_run(
        self,
    ):
        import json
        from unittest.mock import patch

        from review_sensei_github.publication import ReviewPublisher
        from review_sensei_github.session_ledger import parse_session_comment

        with tempfile.TemporaryDirectory() as directory:
            f = EntryFixture(directory)
            with patch.object(
                ReviewPublisher,
                "_finish_thread_cleanup",
                lambda _, value: replace(value, thread_resolution="unconfirmed"),
            ):
                self.assertEqual(f.invoke(human=True), 1, f.error)
            outcome = json.loads((f.root / "publication.json").read_text())
            self.assertEqual(outcome["status"], "published_terminal_pending")
            self.assertEqual(outcome["publication_status"], "published")
            self.assertFalse(outcome["accounting_terminal"])
            self.assertEqual(outcome["terminal_reason"], "thread_cleanup_unconfirmed")
            old = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertEqual(old.transaction.phase, "publication_succeeded")
            self.assertIsNone(old.api_run_accounting["terminal_transaction"])
            f.next_run()
            self.assertEqual(f.invoke(human=True), 1)
            self.assertIn("previous caller run is not terminal", f.error)
            self.assertEqual(f.provider_calls, 1)

    def test_close_refusal_reports_terminal_pending_for_published_and_skipped(self):
        import json
        from unittest.mock import patch

        from review_sensei_github.api_run_requests import ApiRunRequestTransport

        from review_sensei.errors import ReviewInputError

        for skipped, method in (
            (False, "finish_review_run"),
            (True, "finish_review_run"),
            (True, "begin_terminal"),
        ):
            with (
                self.subTest(skipped=skipped, method=method),
                tempfile.TemporaryDirectory() as directory,
            ):
                f = EntryFixture(directory)
                if skipped:
                    self.assertEqual(f.invoke(), 0, f.error)
                    f.next_run()
                    f.requested_head = f.live_head = HEAD
                with patch.object(
                    ApiRunRequestTransport,
                    method,
                    side_effect=ReviewInputError(
                        "synthetic close acknowledgment unknown"
                    ),
                ):
                    self.assertEqual(f.invoke(), 1, f.error)
                outcome = json.loads((f.root / "publication.json").read_text())
                self.assertEqual(outcome["status"], "published_terminal_pending")
                self.assertEqual(
                    outcome["publication_status"],
                    "already_published" if skipped else "published",
                )
                self.assertFalse(outcome["accounting_terminal"])
                self.assertEqual(
                    outcome["terminal_reason"], "terminal_close_unconfirmed"
                )

    def test_unresolved_mutation_response_cannot_close_or_roll_over_run(self):
        from fake_github_http import json_response
        from review_sensei_github.session_ledger import parse_session_comment

        # One extra attempted caller operation per owner in this control only.
        with tempfile.TemporaryDirectory() as directory:
            f = EntryFixture(directory, limit=QUALIFICATION_LIMIT + 2)

            def ignored_optional_failure():
                original = f.accounted._opener

                def unknown(request, timeout):
                    f.trace.append(
                        (
                            f.attempt,
                            request.get_method(),
                            "/repos/owner/repo/issues/7/comments",
                        )
                    )
                    return json_response({"message": "synthetic-only"}, 503)

                f.accounted._opener = unknown
                try:
                    try:
                        f.http.request(
                            "POST",
                            "/repos/owner/repo/issues/7/comments",
                            token="synthetic",
                            body={"body": "synthetic uncertainty"},
                        )
                    except Exception:
                        pass
                finally:
                    f.accounted._opener = original

            f.before_publication = ignored_optional_failure
            self.assertEqual(f.invoke(), 1)
            current = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertEqual(current.transaction.phase, "publication_succeeded")
            self.assertIsNone(current.api_run_accounting["terminal_transaction"])
            self.assertTrue(
                any(o.get("uncertain_effect") for o in f.accounted.observations)
            )
            f.before_publication = None
            f.next_run()
            self.assertEqual(f.invoke(), 1)
            self.assertEqual(f.provider_calls, 1)
            self.assertFalse(
                any(
                    m in {"POST", "PATCH"} and "/issues" in path
                    for _, m, path in f.trace
                )
            )

    def test_retained_whole_root_keeps_decisions_and_baseline_and_reads_are_metered(
        self,
    ):
        from review_sensei_github.api_run_history import read_previous_run
        from review_sensei_github.session_ledger import parse_session_comment

        with tempfile.TemporaryDirectory() as directory:
            f = EntryFixture(directory)
            self.assertEqual(f.invoke(), 0, f.error)
            # Frozen historical disposition input: this is data retention, not
            # a live command or an assertion of new disposition authority.
            disposition = {
                "fingerprint": "abcd1234abcd1234",
                "action": "dismiss",
                "reason": "accepted",
                "actor": "fixture",
                "head_sha": HEAD,
                "expires_at": None,
            }
            f.rewrite_record(
                mutate=lambda r: r.evolve(
                    generation=r.generation + 1, dispositions=(disposition,)
                )
            )
            previous = parse_session_comment(f.comment["body"], identity=IDENTITY)
            f.next_run()
            self.assertEqual(f.invoke(), 0, f.error)
            current = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertEqual(current.dispositions, previous.dispositions)
            before = len(f.trace)
            confirmed = f.accounted.accounting.confirmed
            retained = read_previous_run(
                f.selected.ledger, IDENTITY, f.accounted.accounting.previous_record
            )
            self.assertEqual(retained.to_dict(), previous.to_dict())
            self.assertEqual(len(f.trace), before + 1)
            self.assertEqual(f.accounted.accounting.confirmed, confirmed + 1)
            self.assertEqual(retained.api_run_accounting, previous.api_run_accounting)
            self.assertEqual(retained.convergence_history, previous.convergence_history)

    def test_changed_part_or_wrong_producer_prevents_accounting_transition(self):
        import copy

        for mode in ("bytes", "owner"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                f = EntryFixture(directory)
                self.assertEqual(f.invoke(), 0, f.error)
                previous = copy.deepcopy(f.comment)
                f.next_run()
                f.archive_bad = mode
                self.assertEqual(f.invoke(), 1)
                self.assertEqual(f.comment, previous)
                self.assertEqual(f.provider_calls, 1)
                self.assertFalse(any(method == "PATCH" for _, method, _ in f.trace))

    def test_known_analysis_abort_closes_and_third_run_retains_chain(self):
        from review_sensei_github.api_run_history import read_previous_run
        from review_sensei_github.session_ledger import parse_session_comment

        from review_sensei.errors import ReviewInputError

        with tempfile.TemporaryDirectory() as directory:
            f = EntryFixture(directory)

            def failed():
                raise ReviewInputError("known fixture input refusal")

            f.before_entry = failed
            self.assertEqual(f.invoke(), 1)
            aborted = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertIsNone(aborted.transaction)
            self.assertIsNotNone(aborted.api_run_accounting["analysis_abort"])
            f.before_entry = None
            f.next_run()
            self.assertEqual(f.invoke(), 0, f.error)
            second = parse_session_comment(f.comment["body"], identity=IDENTITY)
            f.next_run()
            f.requested_head = f.live_head = "e" * 40
            self.assertEqual(f.invoke(), 0, f.error)
            retained = read_previous_run(
                f.selected.ledger, IDENTITY, f.accounted.accounting.previous_record
            )
            self.assertEqual(retained.to_dict(), second.to_dict())
            self.assertEqual(
                retained.api_run_accounting["previous_record"]["root_sha256"],
                aborted.record_sha256,
            )

    def test_uncertain_provider_failure_does_not_authorize_rollover(self):
        from review_sensei_github.session_ledger import parse_session_comment

        from review_sensei.errors import ProviderError

        with tempfile.TemporaryDirectory() as directory:
            f = EntryFixture(directory)

            def failed():
                raise ProviderError(
                    "synthetic provider outcome unknown", transient=True
                )

            f.before_provider = failed
            self.assertEqual(f.invoke(), 1)
            record = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertIsNone(record.api_run_accounting["analysis_abort"])
            f.before_provider = None
            f.next_run()
            self.assertEqual(f.invoke(), 1)

    def test_only_confirmed_pre_entry_abort_can_close(self):
        from review_sensei_github.session_ledger import parse_session_comment

        from review_sensei.errors import ReviewInputError

        cases = (
            ("pre-entry", ReviewInputError("local validation refusal"), None, True, 1),
            ("pre-entry", KeyboardInterrupt(), None, True, 130),
            ("provider", ReviewInputError("inside Provider.complete"), None, False, 1),
            ("provider", KeyboardInterrupt(), None, False, 130),
            ("provider", TimeoutError("provider timed out"), None, False, 1),
            (
                "pre-entry",
                ReviewInputError("local validation refusal"),
                "cleanup-absent",
                False,
                1,
            ),
            (
                "pre-entry",
                ReviewInputError("local validation refusal"),
                "closure-stored",
                True,
                1,
            ),
            (
                "pre-entry",
                ReviewInputError("local validation refusal"),
                "closure-absent",
                False,
                1,
            ),
        )
        for phase, error, fault, closes, code in cases:
            with (
                self.subTest(phase=phase, error=type(error).__name__, fault=fault),
                tempfile.TemporaryDirectory() as directory,
            ):
                f = EntryFixture(directory)

                def refuse():
                    raise error

                if phase == "pre-entry":
                    f.before_entry = refuse
                else:
                    f.before_provider = refuse
                f.abort_fault = fault
                self.assertEqual(f.invoke(), code, f.error)
                record = parse_session_comment(f.comment["body"], identity=IDENTITY)
                self.assertEqual(
                    record.api_run_accounting["analysis_abort"] is not None, closes
                )
                old = f.accounted.accounting
                if closes:
                    closure = old.analysis_abort
                    self.assertEqual(closure["run_id"], f.run_id)
                    self.assertEqual(closure["owner"]["run_attempt"], 1)
                    self.assertEqual(closure["entry_state"], "not_started")
                f.before_entry = f.before_provider = None
                f.next_run()
                self.assertEqual(f.invoke(), 0 if closes else 1, f.error)
                if closes:
                    from review_sensei_github.api_run_history import read_previous_run

                    archived = read_previous_run(
                        f.selected.ledger,
                        IDENTITY,
                        f.accounted.accounting.previous_record,
                    )
                    self.assertEqual(archived.to_dict(), record.to_dict())
