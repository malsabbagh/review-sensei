"""The public command reconstructs retained state and reaches native approval.

The ordinary suite uses a synthetic broker HTTP protocol boundary. Set
REVIEWSENSEI_MEDIA_BROKER_QUALIFICATION=1 to run the same journeys through the
production TypeScript broker, signed OIDC, and independently traced physical
dispatches. Neither lane mocks the application, canonical loader or publishers.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import queue
import subprocess
import tempfile
import threading
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

from fake_github_http import json_response
from review_sensei_github import action_run, review_run
from review_sensei_github.api_run_host import review_startup_plan
from review_sensei_github.api_run_requests import ApiRunRequestTransport
from review_sensei_github.broker_client import BrokerClient
from review_sensei_github.checks import REVIEW_CHECK_NAME
from review_sensei_github.cli import main as github_main
from review_sensei_github.http import GitHubHttp
from review_sensei_github.human_file_review import (
    RECEIPT_GROUP_PREFIX,
    RECEIPT_PREFIX,
    REQUEST_GROUP_PREFIX,
    REQUEST_PREFIX,
)
from review_sensei_github.operation_host import RESULT_COMMENT_MARKER
from review_sensei_github.run_authority import parse_authority
from review_sensei_github.session_ledger import (
    parse_session_comment,
    render_session_comment,
)
from test_github_human_file_review import ALICE, BOT, HEAD, REPO
from test_hosted_media_context import BODY, IDENTITY, HostedMediaState
from test_review_run import fixture_process_environment

from review_sensei.api_work import (
    ApiExecutionOwner,
    ApiRunIdentity,
    FrozenApiConfiguration,
    initial_accounting,
)
from review_sensei.bounded_evidence import EvidenceReadBudget

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = (
    "malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@refs/tags/v5"
)


class MediaCLIState(HostedMediaState):
    """Mutable synthetic external GitHub, including the terminal merge check."""

    def __init__(self, **kwargs):
        self.check_runs = [
            {
                "id": 700,
                "name": REVIEW_CHECK_NAME,
                "head_sha": HEAD,
                "app": {"slug": "reviewsensei"},
                "status": "completed",
                "conclusion": "action_required",
            }
        ]
        self.dispatched_writes = []
        self.fail_check = False
        self.lose_review_response = False
        self.refuse_read_after_receipt = False
        self.confirmed_read_refusals = 0
        super().__init__(**kwargs)
        self.dispatched_writes.clear()

    def open(self, request, timeout):
        path = urlsplit(request.full_url).path
        if request.method != "GET" and path != "/graphql":
            self.dispatched_writes.append(
                (request.method, path, json.loads(request.data))
            )
        if (
            self.refuse_read_after_receipt
            and request.method == "GET"
            and path == REPO + "/pulls/2"
            and any(
                prefix in item["body"]
                for item in self.comments.values()
                for prefix in (RECEIPT_PREFIX, RECEIPT_GROUP_PREFIX)
            )
        ):
            # Seed a real retained receipt, then stop at a confirmed read refusal
            # before approval. Worker fanout no longer exhausts caller capacity.
            self.physical.append(
                (request.method, path, request.get_header("Authorization"), timeout)
            )
            self.refuse_read_after_receipt = False
            self.confirmed_read_refusals += 1
            return json_response({"message": "synthetic confirmed read refusal"}, 403)
        lookup = f"{REPO}/commits/{HEAD}/check-runs"
        if path == lookup or path.startswith(REPO + "/check-runs"):
            self.physical.append(
                (request.method, path, request.get_header("Authorization"), timeout)
            )
            if self.fail_check:
                return json_response({"message": "synthetic transient refusal"}, 500)
            if request.method == "GET":
                return json_response({"check_runs": copy.deepcopy(self.check_runs)})
            body = json.loads(request.data)
            if request.method == "PATCH":
                identifier = int(path.rsplit("/", 1)[1])
                target = next(x for x in self.check_runs if x["id"] == identifier)
                target.update(body)
                return json_response(copy.deepcopy(target))
            if request.method == "POST":
                target = body | {"id": 701, "app": {"slug": "reviewsensei"}}
                self.check_runs.append(target)
                return json_response(copy.deepcopy(target), 201)
            raise AssertionError("unexpected check fixture operation")
        response = super().open(request, timeout)
        if (
            self.lose_review_response
            and request.method == "POST"
            and path == REPO + "/pulls/2/reviews"
        ):
            self.lose_review_response = False
            raise URLError("synthetic dropped native publication response")
        return response

    def broker_authority(self):
        return {
            "base": self.base,
            "comments": list(copy.deepcopy(self.comments).values()),
            "head": self.head,
            "draft": self.draft,
            "fork": self.fork,
            "source": copy.deepcopy(self.comments.get(20)),
            "reviews": [
                {
                    key: copy.deepcopy(value[key])
                    for key in ("id", "user", "commit_id", "state")
                }
                for value in self.reviews
            ],
        }


class SyntheticBrokerProtocol:
    """Synthetic external broker response; Python protocol/accounting stays real."""

    def __init__(self, owner):
        self.owner = owner
        self.requests = []
        self.admitted = []
        self.receipts = []

    def oidc(self):
        return "synthetic-fixture-oidc"

    def cold_invocation(self):
        pass

    def open(self, request, timeout):
        assert 0 < timeout <= 60
        payload = json.loads(request.data)
        verifying = "session_grant" in payload
        capability = "verify" if verifying else payload["capability"]
        self.requests.append(capability)
        self.admitted.append(payload["control_budget"]["max_dispatches"])
        authority = parse_authority(payload["session_attestation"], grant=verifying)
        state = self.owner.state
        source = state.comments.get(20)
        scope = authority["source"]
        if (
            authority["purpose"] != "command"
            or authority["job"] != "command"
            or authority["run_id"] != str(self.owner.run_id)
            or authority["run_attempt"] != self.owner.attempt
            or state.head != HEAD
            or source is None
            or source["user"] != ALICE
            or source.get("author_association") != "COLLABORATOR"
            or scope["id"] != source["id"]
            or scope["updated_at"] != source["updated_at"]
            or scope["body_sha256"]
            != hashlib.sha256(source["body"].encode()).hexdigest()
        ):
            raise HTTPError(
                request.full_url, 403, "synthetic refusal", {}, io.BytesIO(b"{}")
            )
        capability = "verify" if verifying else payload["capability"]
        control = payload["control_budget"]
        receipt = control | {"dispatched": min(9, control["max_dispatches"])}
        self.receipts.append(receipt)
        actor = {"login": ALICE["login"], "type": "User", "association": "COLLABORATOR"}
        if verifying:
            assert authority["actor"] == actor
            return json_response(
                {"session_attestation": authority, "control_receipt": receipt}
            )
        result = {
            "token": "synthetic-scoped",
            "capability": capability,
            "control_receipt": receipt,
        }
        if capability == "review_session":
            result.update(
                app_id=999,
                session_state="known",
                reservation_owner={"run_id": authority["run_id"], "head_sha": HEAD},
                session_grant="sg1.9999999999999." + "A" * 43,
                session_attestation=authority | {"actor": actor},
            )
        return json_response(result)

    def close(self):
        pass


class ProductionMediaBrokerBridge(SyntheticBrokerProtocol):
    """Run actual Worker policy/signatures and count every synthetic fetch."""

    def __init__(self, owner):
        super().__init__(owner)
        self.sequence = 0
        self.attempts = []
        self.replies = queue.Queue()
        environment = {
            key: value
            for key, value in os.environ.items()
            if key in {"PATH", "NODE_PATH", "SystemRoot", "TMPDIR", "TEMP", "TMP"}
        }
        self.process = subprocess.Popen(
            ["node", str(ROOT / "tests/media_cli_broker_bridge.mjs")],
            cwd=ROOT,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        def read():
            for line in self.process.stdout:
                self.replies.put(json.loads(line))
            self.replies.put(None)

        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()

    def call(self, action, **arguments):
        self.sequence += 1
        self.process.stdin.write(
            json.dumps({"id": self.sequence, "action": action, **arguments}) + "\n"
        )
        self.process.stdin.flush()
        result = self.replies.get(timeout=20)
        if result is None:
            raise RuntimeError(
                "production media broker bridge exited: "
                + self.process.stderr.read()[:2000]
            )
        if result["id"] != self.sequence:
            raise RuntimeError("production media broker reply changed identity")
        self.attempts.extend(result["attempts"])
        return result

    def oidc(self):
        reply = self.call(
            "oidc", run_id=str(self.owner.run_id), run_attempt=self.owner.attempt
        )
        assert reply["status"] == 200
        return reply["result"]

    def cold_invocation(self):
        reply = self.call("reset_oidc_cache")
        assert reply["status"] == 200

    def open(self, request, timeout):
        assert 0 < timeout <= 60
        payload = json.loads(request.data)
        self.requests.append(
            "verify" if "session_grant" in payload else payload["capability"]
        )
        self.admitted.append(payload["control_budget"]["max_dispatches"])
        self.attempts.append(
            {
                "origin": "caller-broker",
                "method": request.method,
                "path": urlsplit(request.full_url).path,
            }
        )
        reply = self.call(
            "verify" if "session_grant" in payload else "exchange",
            body=payload,
            authority=self.owner.state.broker_authority(),
        )
        # Every fixture fetch is traced, including failures and cold auth. Keep
        # the service ceiling independent from the one caller POST above.
        self.owner.assertTrue(
            all(item["origin"] == "broker-internal" for item in reply["attempts"])
        )
        self.owner.assertLessEqual(
            len(reply["attempts"]), payload["control_budget"]["max_dispatches"]
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
        self.owner.assertEqual(len(reply["attempts"]), self.receipts[-1]["dispatched"])
        return json_response(reply["result"])

    def close(self):
        try:
            if self.process.poll() is None:
                self.call("shutdown")
        finally:
            if self.process.poll() is None:
                self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
            self.reader.join(timeout=1)
            for stream in (
                self.process.stdin,
                self.process.stdout,
                self.process.stderr,
            ):
                if stream is not None:
                    stream.close()


class MediaCLIActivationTests(unittest.TestCase):
    production_broker = False

    def setUp(self):
        self.state = MediaCLIState()
        self.budgets = []
        self.budget_starts = []
        self.broker = (
            ProductionMediaBrokerBridge(self)
            if self.production_broker
            else SyntheticBrokerProtocol(self)
        )
        self.addCleanup(self.broker.close)

    def activate(self, *, policy="auto-approve"):
        import base64

        # The native published source is a current closed caller record. Its
        # authenticated synthetic seed is fixture setup, not a legacy fallback.
        if getattr(self, "seeded_state", None) is not self.state:
            self.seeded_state = self.state
            self.run_id, self.attempt = 123, 1
            self.scratch = tempfile.TemporaryDirectory()
            self.addCleanup(self.scratch.cleanup)
            self.run_root = Path(self.scratch.name)
            self.config_bytes = (
                f"schema: 1\napi_request_limit: 1024\ngithub:\n  writes: true\n  reviews: {policy}\n"
            ).encode()
            config_path = self.run_root / ".reviewsensei.yml"
            config_path.write_bytes(self.config_bytes)
            for command in (
                ["git", "init", str(self.run_root)],
                ["git", "-C", str(self.run_root), "add", ".reviewsensei.yml"],
                [
                    "git",
                    "-C",
                    str(self.run_root),
                    "-c",
                    "user.name=Fixture",
                    "-c",
                    "user.email=fixture@example.invalid",
                    "commit",
                    "-m",
                    "fixture",
                ],
            ):
                subprocess.run(command, check=True, capture_output=True)
            self.policy = subprocess.check_output(
                ["git", "-C", str(self.run_root), "rev-parse", "HEAD"], text=True
            ).strip()
            record = parse_session_comment(
                self.state.comments[600]["body"], identity=IDENTITY
            )
            accounting = initial_accounting(
                ApiRunIdentity(1, 122),
                FrozenApiConfiguration(
                    self.policy, hashlib.sha256(self.config_bytes).hexdigest(), 1024
                ),
                ApiExecutionOwner(1, "hosted", 0),
                review_startup_plan("hosted"),
                prior_nonstartup_liability_upper_bound=0,
            )
            accounting = replace(
                accounting, terminal_transaction=record.transaction.transaction_id
            )
            self.state.comments[600]["body"] = render_session_comment(
                repository_id=1,
                pull_request=2,
                record=record.evolve(
                    generation=record.generation + 1,
                    api_run_accounting=accounting.to_document(),
                ),
            )
        else:
            previous = parse_session_comment(
                self.state.comments[600]["body"], identity=IDENTITY
            )
            current = previous.api_run_accounting
            if (
                current["action"] is not None
                and current["action"]["phase"] == "terminal"
            ):
                self.run_id += 1
                self.attempt = 1
            else:
                self.attempt += 1
        config_path = self.run_root / ".reviewsensei.yml"
        event_file = self.run_root / "event.json"
        event_file.write_text(
            json.dumps(
                {
                    "issue": {"number": 2},
                    "comment": {
                        "id": 20,
                        "body": BODY,
                        "updated_at": "2026-10-22T01:00:00Z",
                    },
                }
            )
        )
        self.current_caller = None
        self.selected = None

        def caller_factory(**kwargs):
            self.current_caller = ApiRunRequestTransport(**kwargs)
            return self.current_caller

        def broker_factory(**kwargs):
            return BrokerClient(
                broker_url="https://broker.github.test/api/github/token", **kwargs
            )

        def http_factory(**kwargs):
            return GitHubHttp(api_url="https://api.github.test", **kwargs)

        original_bootstrap = action_run.bootstrap

        def bootstrap(*args, **kwargs):
            run = original_bootstrap(*args, **kwargs)
            self.selected = run
            self.budgets.append(run.caller.budget_view)
            return run

        def physical(request, timeout):
            target = urlsplit(request.full_url)
            path = target.path
            if target.hostname == "broker.github.test":
                return self.broker.open(request, timeout)
            special = (
                target.hostname == "oidc.github.test"
                or path in (REPO, REPO + "/git/ref/heads/main")
                or "/contents/.reviewsensei.yml" in path
                or "/actions/runs/" in path
                or request.method == "PATCH"
                and "/issues/comments/" in path
            )
            if not special:
                response = self.state.open(request, timeout)
                if (
                    path == REPO + "/pulls/2"
                    and request.method == "GET"
                    and response.status == 200
                ):
                    pull = json.loads(response.read(524288))
                    pull["base"]["ref"] = "main"
                    return json_response(pull)
                return response
            self.state.physical.append(
                (request.method, path, request.get_header("Authorization"), timeout)
            )
            if target.hostname == "oidc.github.test":
                return json_response({"value": self.broker.oidc()})
            if path == REPO:
                return json_response(
                    {
                        "id": 1,
                        "full_name": "owner/repo",
                        "fork": self.state.fork,
                        "default_branch": "main",
                    }
                )
            if path.endswith("/git/ref/heads/main"):
                return json_response({"object": {"type": "commit", "sha": self.policy}})
            if "/contents/.reviewsensei.yml" in path:
                return json_response(
                    {
                        "type": "file",
                        "encoding": "base64",
                        "size": len(self.config_bytes),
                        "content": base64.b64encode(self.config_bytes).decode(),
                    }
                )
            if "/actions/runs/" in path:
                identifier = int(path.split("/actions/runs/")[1].split("/")[0])
                current = identifier == self.run_id
                if path.endswith("/jobs"):
                    attempt = int(path.split("/attempts/")[1].split("/")[0])
                    active = current and attempt == self.attempt
                    return json_response(
                        {
                            "total_count": 1,
                            "jobs": [
                                {
                                    "id": identifier * 10 + attempt,
                                    "run_id": identifier,
                                    "run_attempt": attempt,
                                    "name": "ReviewSensei / "
                                    + ("hosted" if identifier == 122 else "command"),
                                    "status": "in_progress" if active else "completed",
                                    "conclusion": None if active else "success",
                                    "completed_at": None
                                    if active
                                    else "2026-10-26T00:00:00Z",
                                }
                            ],
                        }
                    )
                return json_response(
                    {
                        "id": identifier,
                        "run_attempt": self.attempt if current else 1,
                        "repository": {"id": 1, "full_name": "owner/repo"},
                        "status": "in_progress" if current else "completed",
                        "event": "issue_comment" if current else "pull_request",
                    }
                )
            identifier = int(path.rsplit("/", 1)[1])
            self.state.comments[identifier]["body"] = json.loads(request.data)["body"]
            return json_response(copy.deepcopy(self.state.comments[identifier]))

        output, errors = io.StringIO(), io.StringIO()
        start_http, start_broker = len(self.state.physical), len(self.broker.requests)
        start_admitted = len(self.broker.admitted)
        start_trace = len(getattr(self.broker, "attempts", ()))
        previous_directory = Path.cwd()
        try:
            os.chdir(self.run_root)
            with ExitStack() as stack:
                for scope in (
                    patch.dict(
                        os.environ,
                        {
                            **fixture_process_environment(),
                            "GITHUB_TOKEN": "synthetic-read",
                            "GITHUB_RUN_ID": str(self.run_id),
                            "GITHUB_RUN_ATTEMPT": str(self.attempt),
                            "GITHUB_EVENT_NAME": "issue_comment",
                            "GITHUB_EVENT_PATH": str(event_file),
                            "GITHUB_ACTOR": "alice",
                            "GITHUB_TRIGGERING_ACTOR": "alice",
                            "REVIEW_SENSEI_WORKFLOW_REF": WORKFLOW,
                            "REVIEW_SENSEI_WORKFLOW_SHA": "a" * 40,
                            "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "synthetic",
                            "ACTIONS_ID_TOKEN_REQUEST_URL": "https://oidc.github.test/token?fixture=1",
                        },
                        clear=True,
                    ),
                    patch.object(review_run, "physical_opener", physical),
                    patch.object(
                        review_run, "ApiRunRequestTransport", side_effect=caller_factory
                    ),
                    patch.object(
                        review_run, "BrokerClient", side_effect=broker_factory
                    ),
                    patch.object(review_run, "GitHubHttp", side_effect=http_factory),
                    patch.object(action_run, "bootstrap", side_effect=bootstrap),
                    patch.object(
                        EvidenceReadBudget,
                        "__init__",
                        side_effect=AssertionError("no legacy phase budget"),
                    ),
                    redirect_stdout(output),
                    redirect_stderr(errors),
                ):
                    stack.enter_context(scope)
                status = github_main(
                    [
                        "command-run",
                        "--policy-commit",
                        self.policy,
                        "--run-directory",
                        str(self.run_root),
                        "--",
                        "--comment-body",
                        BODY,
                        "--actor",
                        ALICE["login"],
                        "--association",
                        "COLLABORATOR",
                        "--repository",
                        "owner/repo",
                        "--repository-id",
                        "1",
                        "--pull-request",
                        "2",
                        "--head-sha",
                        HEAD,
                        "--source-comment-id",
                        "20",
                        "--app-slug",
                        BOT["login"],
                        "--config",
                        str(config_path),
                        "--github-session-ledger",
                        "--allow-write",
                    ]
                )
        finally:
            os.chdir(previous_directory)
            self.last_output = output.getvalue() + errors.getvalue()
        self.last_http = len(self.state.physical) - start_http
        self.last_admitted = sum(self.broker.admitted[start_admitted:])
        self.last_broker_attempts = len(self.broker.requests) - start_broker
        trace = getattr(self.broker, "attempts", ())[start_trace:]
        self.last_caller_trace = sum(x["origin"] == "caller-broker" for x in trace)
        self.last_worker_trace = sum(x["origin"] == "broker-internal" for x in trace)
        self.assertEqual(len(trace), self.last_caller_trace + self.last_worker_trace)
        self.last_physical = self.last_http + self.last_broker_attempts
        return status

    def approvals(self):
        return [value for value in self.state.reviews if value["state"] == "APPROVED"]

    def receipt_count(self):
        return sum(
            any(
                prefix in value["body"]
                for prefix in (RECEIPT_PREFIX, RECEIPT_GROUP_PREFIX)
            )
            for value in self.state.comments.values()
        )

    def assert_success(self):
        self.assertEqual(len(self.approvals()), 1)
        self.assertEqual(self.approvals()[0]["commit_id"], HEAD)
        self.assertEqual(self.state.check_runs[0]["status"], "completed")
        self.assertEqual(self.state.check_runs[0]["conclusion"], "success")
        self.assertEqual(self.state.result.review_status, "partial")
        self.assertEqual(self.receipt_count(), 1)
        self.assert_caller_accounting()
        record = parse_session_comment(
            self.state.comments[600]["body"], identity=IDENTITY
        )
        self.assertEqual(record.api_run_accounting["action"]["phase"], "terminal")
        self.assertEqual(self.broker.requests[-1], "check_publish")
        self.assertTrue(
            all(0 < r["max_dispatches"] <= 60 for r in self.broker.receipts)
        )
        self.assertIn("media-approved", self.last_output)

    def assert_no_media_effects(self):
        # Rollover may write the immutable accounting archive before the action
        # rejects. Only that exact archive carrier is allowed here.
        for method, path, payload in self.state.dispatched_writes:
            self.assertEqual((method, path), ("POST", REPO + "/issues/2/comments"))
            self.assertTrue(
                payload["body"].startswith("ReviewSensei immutable evidence part v1\n")
            )
        self.assertEqual(self.receipt_count(), 0)
        self.assertFalse(self.approvals())

    def assert_caller_accounting(self):
        self.assertEqual(
            len(self.current_caller.observations),
            self.last_http + self.last_broker_attempts,
        )
        if self.production_broker:
            self.assertEqual(self.last_caller_trace, self.last_broker_attempts)
            self.assertLessEqual(self.last_worker_trace, self.last_admitted)

    def retain_media_receipt_before_approval(self):
        self.broker.cold_invocation()
        before = self.state.confirmed_read_refusals
        review_posts = sum(
            method == "POST" and path == REPO + "/pulls/2/reviews"
            for method, path, _, _ in self.state.physical
        )
        self.state.refuse_read_after_receipt = True
        self.assertEqual(self.activate(), 1, self.last_output)
        self.assertEqual(self.state.confirmed_read_refusals, before + 1)
        self.assertFalse(self.state.refuse_read_after_receipt)
        self.assertEqual(
            sum(
                method == "POST" and path == REPO + "/pulls/2/reviews"
                for method, path, _, _ in self.state.physical
            ),
            review_posts,
        )
        self.assertEqual(self.approvals(), [])
        self.assertEqual(self.receipt_count(), 1)
        self.assertEqual(self.state.check_runs[0]["conclusion"], "action_required")
        self.assertLessEqual(self.budgets[-1].calls, 1024)
        self.assert_caller_accounting()
        if self.production_broker:
            self.assertEqual(self.last_physical, len(self.current_caller.observations))

    def test_supported_command_loads_actual_retention_and_finishes_the_check(self):
        for media_count, text_count in ((2, 1), (41, 61)):
            with self.subTest(media_count=media_count):
                self.state = MediaCLIState(
                    media_count=media_count,
                    text_count=text_count,
                    long_text_paths=media_count == 41,
                    nested_media=media_count == 41,
                    oversized_root=media_count == 41,
                )
                if media_count == 41:
                    self.assertEqual(
                        sum(
                            RESULT_COMMENT_MARKER in value["body"]
                            for value in self.state.comments.values()
                        ),
                        2,
                    )
                original_source = copy.deepcopy(self.state.comments[20])
                self.broker.cold_invocation()
                self.assertEqual(self.activate(), 0, self.last_output)
                self.assert_success()
                prefix = REQUEST_PREFIX if media_count == 2 else REQUEST_GROUP_PREFIX
                self.assertEqual(
                    sum(prefix in x["body"] for x in self.state.comments.values()), 1
                )
                if media_count == 41:
                    media_paths = {
                        item.path
                        for item in self.state.result.coverage.files
                        if item.reason == "binary"
                    }
                    self.assertEqual(len(media_paths), 41)
                    self.assertEqual(
                        {path.rsplit("/", 1)[0] for path in media_paths},
                        {
                            "docs/execution/quieter-workspace/after/desktop",
                            "docs/execution/quieter-workspace/after/mobile",
                            "docs/execution/quieter-workspace/before/desktop",
                            "docs/execution/quieter-workspace/before/mobile",
                            "docs/execution/quieter-workspace/zoom",
                        },
                    )
                    self.assertEqual(len(self.state.tree_requests), 4)
                    self.assertEqual(
                        sorted(recursive for _, recursive in self.state.tree_requests),
                        [False] * 2 + [True] * 2,
                    )
                    self.assertTrue(
                        all(
                            not recursive
                            for identifier, recursive in self.state.tree_requests
                            if identifier in {"1" * 40, "2" * 40}
                        )
                    )
                    self.broker.cold_invocation()
                    self.assertEqual(self.activate(), 0, self.last_output)
                    self.assert_success()
                    self.assertEqual(self.state.comments[20], original_source)
                    if self.production_broker:
                        self.assertLessEqual(
                            self.last_physical, len(self.current_caller.observations)
                        )

    def test_nested_recursive_tree_proof_refuses_truncated_or_missing_ancestors(self):
        for field, value in (
            ("recursive_tree_truncated", True),
            ("recursive_tree_missing_ancestor", "execution/quieter-workspace/after"),
        ):
            with self.subTest(field=field):
                self.state = MediaCLIState(
                    media_count=41,
                    text_count=61,
                    long_text_paths=True,
                    nested_media=True,
                    oversized_root=True,
                )
                setattr(self.state, field, value)
                self.assertEqual(self.activate(), 1, self.last_output)
                self.assertEqual(self.approvals(), [])
                self.assertEqual(self.receipt_count(), 0)
                self.assertEqual(
                    self.state.check_runs[0]["conclusion"], "action_required"
                )
                self.assert_no_media_effects()
                expected = 2 if field == "recursive_tree_truncated" else 4
                self.assertEqual(len(self.state.tree_requests), expected)
                self.assertEqual(
                    sorted(recursive for _, recursive in self.state.tree_requests),
                    [False] * (expected // 2) + [True] * (expected // 2),
                )
                self.assertTrue(
                    all(
                        not recursive
                        for identifier, recursive in self.state.tree_requests
                        if identifier in {"1" * 40, "2" * 40}
                    )
                )
                self.assertLessEqual(self.budgets[-1].calls, 1024)
                self.assert_caller_accounting()
                if self.production_broker:
                    self.assertEqual(
                        self.last_physical, len(self.current_caller.observations)
                    )

    def test_successful_explicit_command_is_idempotent_in_a_fresh_run(self):
        self.assertEqual(self.activate(), 0, self.last_output)
        self.assert_success()
        self.assertEqual(self.activate(), 0, self.last_output)
        self.assert_success()

    def assert_pending_successor_refuses_without_duplicate_effect(self):
        writes = len(self.state.reviews)
        receipts = self.receipt_count()
        self.broker.cold_invocation()
        self.assertEqual(self.activate(), 1, self.last_output)
        self.assertIn("requires exact result recovery", self.last_output)
        self.assertEqual(len(self.state.reviews), writes)
        self.assertEqual(self.receipt_count(), receipts)
        self.assert_caller_accounting()
        record = parse_session_comment(
            self.state.comments[600]["body"], identity=IDENTITY
        )
        self.assertEqual(record.api_run_accounting["action"]["phase"], "pending")

    def test_confirmed_read_refusal_after_receipt_keeps_action_pending_without_replay(
        self,
    ):
        self.retain_media_receipt_before_approval()
        self.assert_pending_successor_refuses_without_duplicate_effect()

    def test_optional_and_resolved_human_admission_replay_finishes_native_check(self):
        for human in (False, True):
            with self.subTest(human=human):
                self.state = MediaCLIState(
                    blocking_finding=True,
                    admitted_blocking=False,
                    admitted_human=human,
                    human_resolved=human,
                )
                self.broker.cold_invocation()
                self.assertEqual(self.activate(), 0, self.last_output)
                self.assert_success()
                self.assertTrue(self.state.result.comments[0].blocks_approval)
                self.assertEqual(
                    sum(
                        method == "POST" and path == REPO + "/pulls/2/reviews"
                        for method, path, _, _ in self.state.physical
                    ),
                    1,
                )

    def test_documented_review_200_finishes_the_check_without_response_replay(self):
        self.assertEqual(self.state.review_response_status, 200)
        self.assertEqual(self.activate(), 0, self.last_output)
        self.assert_success()
        self.assertEqual(
            sum(
                method == "POST" and path == REPO + "/pulls/2/reviews"
                for method, path, _, _ in self.state.physical
            ),
            1,
        )
        self.assertEqual(self.state.result.review_status, "partial")

    def test_lost_native_write_response_preserves_approval_and_refuses_automatic_replay(
        self,
    ):
        self.state.lose_review_response = True
        self.assertEqual(self.activate(), 1, self.last_output)
        self.assertEqual(len(self.approvals()), 1)
        self.assertEqual(self.state.check_runs[0]["conclusion"], "action_required")
        self.assert_pending_successor_refuses_without_duplicate_effect()
        self.assertEqual(
            sum(
                method == "POST" and path == REPO + "/pulls/2/reviews"
                for method, path, _, _ in self.state.physical
            ),
            1,
        )

    def test_failed_check_preserves_approval_and_refuses_automatic_replay(self):
        self.state.fail_check = True
        self.assertEqual(self.activate(), 1, self.last_output)
        self.assertEqual(len(self.approvals()), 1)
        self.assertEqual(self.state.check_runs[0]["conclusion"], "action_required")
        self.state.fail_check = False
        self.assert_pending_successor_refuses_without_duplicate_effect()

    def test_current_maintainer_and_head_binding_refuse_before_media_writes(self):
        for field, value in (("permission", "write"), ("head", "c" * 40)):
            with self.subTest(field=field):
                self.state = MediaCLIState()
                setattr(self.state, field, value)
                self.assertEqual(self.activate(), 1, self.last_output)
                self.assertEqual(self.approvals(), [])
                self.assertEqual(self.receipt_count(), 0)
                self.assert_no_media_effects()

    def test_changed_source_identity_and_command_refuse_before_media_writes(self):
        for field, value in (
            ("body", BODY + " extra prose"),
            ("user", ALICE | {"id": 987}),
            ("user", ALICE | {"type": "Bot"}),
            ("author_association", "NONE"),
        ):
            with self.subTest(field=field, value=value):
                self.state = MediaCLIState()
                self.state.comments[20][field] = value
                self.assertEqual(self.activate(), 1, self.last_output)
                self.assertEqual(self.approvals(), [])
                self.assertEqual(self.receipt_count(), 0)
                self.assert_no_media_effects()

    def test_missing_owned_retained_result_refuses_before_media_writes(self):
        retained = next(
            value
            for value in self.state.comments.values()
            if RESULT_COMMENT_MARKER in value["body"]
        )
        del self.state.comments[retained["id"]]
        self.assertEqual(self.activate(), 1, self.last_output)
        self.assertEqual(self.approvals(), [])
        self.assertEqual(self.receipt_count(), 0)
        self.assert_no_media_effects()

    def test_blocking_policy_records_human_coverage_without_native_approval(self):
        self.assertEqual(self.activate(policy="blocking"), 0, self.last_output)
        self.assertEqual(self.approvals(), [])
        self.assertEqual(self.receipt_count(), 1)
        self.assertEqual(self.state.check_runs[0]["conclusion"], "success")
        self.assertEqual(self.state.result.review_status, "partial")


@unittest.skipUnless(
    os.environ.get("REVIEWSENSEI_MEDIA_BROKER_QUALIFICATION") == "1",
    "explicit production broker qualification requires Node and pinned Worker dependencies",
)
class ProductionMediaCLIActivationTests(MediaCLIActivationTests):
    production_broker = True

    def test_separate_cold_annotated_fallback_check_finishes_media_and_check(self):
        original_call = self.broker.call

        def isolated_check_worker(action, **arguments):
            if (
                action == "exchange"
                and arguments["body"]["capability"] == "check_publish"
            ):
                original_call("reset_oidc_cache")
                arguments["annotated_check_fallback"] = True
            return original_call(action, **arguments)

        self.broker.call = isolated_check_worker
        self.broker.cold_invocation()
        self.assertEqual(self.activate(), 0, self.last_output)
        self.assert_success()
        checks = [
            receipt
            for name, receipt in zip(self.broker.requests, self.broker.receipts)
            if name == "check_publish"
        ]
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0]["max_dispatches"], 60)
        self.assertEqual(checks[0]["dispatched"], 11)
        self.assertLessEqual(checks[0]["dispatched"], checks[0]["max_dispatches"])
        self.assertGreaterEqual(
            sum(
                value["path"].endswith("/.well-known/jwks")
                for value in self.broker.attempts
            ),
            2,
        )


if __name__ == "__main__":
    unittest.main()
