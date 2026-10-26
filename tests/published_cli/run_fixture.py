"""Installed current CLI; only external HTTP, clocks and transport construction vary.

Reuse source-owned synthetic GitHub/broker response fixtures. Never import either
src tree, replace a lifecycle continuation, or manufacture an admission result.
"""

import copy
import io
import json
import os
import socket
import subprocess
import sys
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

TESTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TESTS))
from packaging_guard import assert_distribution_import  # noqa: E402

assert_distribution_import()
from fake_github_http import json_response  # noqa: E402
from review_sensei_github import cli, review_run  # noqa: E402
from review_sensei_github.api_run_requests import ApiRunRequestTransport  # noqa: E402
from review_sensei_github.broker_client import BrokerClient  # noqa: E402
from review_sensei_github.http import GitHubHttp  # noqa: E402
from review_sensei_github.session_ledger import parse_session_comment  # noqa: E402
from test_action_reply_entry import ReplyEntryFixture  # noqa: E402
from test_api_work_persistence import BASE, IDENTITY  # noqa: E402
from test_api_work_persistence import HEAD as HEAD  # noqa: E402
from test_review_run import EntryFixture, fixture_process_environment  # noqa: E402

from review_sensei.providers.ollama import OllamaProvider  # noqa: E402

assert Path(cli.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())


def commit_policy(fixture):
    (fixture.root / ".reviewsensei.yml").write_bytes(fixture.config_bytes)
    subprocess.run(
        ["git", "add", ".reviewsensei.yml"],
        cwd=fixture.root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            "synthetic policy",
        ],
        cwd=fixture.root,
        check=True,
        capture_output=True,
    )
    fixture.policy = fixture.policy_ref = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=fixture.root, text=True
    ).strip()


def invoke(fixture, *, reply=False, token=True, appid="99", runid=True):
    """Call the real installed CLI, broker client, caller meter and publisher."""
    event = fixture.root / "event.json"
    if reply:
        event.write_text(
            json.dumps(
                {
                    "issue": {"number": 7},
                    "comment": fixture.source,
                    "repository": {"id": 123, "full_name": "owner/repo"},
                    "sender": fixture.source["user"],
                }
            )
        )
    environment = {
        **fixture_process_environment(),
        "GITHUB_RUN_ID": str(fixture.run_id) if runid else "",
        "GITHUB_RUN_ATTEMPT": str(fixture.attempt),
        "GITHUB_EVENT_NAME": "issue_comment" if reply else "pull_request",
        "GITHUB_EVENT_PATH": str(event),
        "GITHUB_ACTOR": "alice",
        "GITHUB_TRIGGERING_ACTOR": "alice",
        "REVIEW_SENSEI_WORKFLOW_REF": "malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@refs/tags/v5",
        "REVIEW_SENSEI_WORKFLOW_SHA": "a" * 40,
        "REVIEWSENSEI_APP_ID": appid,
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "synthetic",
        "ACTIONS_ID_TOKEN_REQUEST_URL": "https://oidc.github.test/token?fixture=1",
        "OLLAMA_API_KEY": "synthetic-only",
    }
    if token:
        environment["GITHUB_TOKEN"] = "synthetic-workflow-read"

    def caller(**kwargs):
        selected = ApiRunRequestTransport(
            **kwargs,
            clock=lambda: 1793000000 + fixture.clock,
            monotonic=lambda: fixture.clock,
        )
        fixture.accounted = selected
        fixture.transports.append(selected)
        return selected

    def http(**kwargs):
        fixture.http = GitHubHttp(api_url="https://api.github.test", **kwargs)
        return fixture.http

    def provider_open(request, *, timeout, **kwargs):
        fixture.provider_calls += 1
        payload = json.loads(request.data)
        document = (
            fixture.reply_document
            if reply
            else {"summary": "Synthetic complete review.", "comments": []}
        )
        return io.BytesIO(
            json.dumps(
                {
                    "model": payload["model"],
                    "response": json.dumps(document),
                    "done": not getattr(fixture, "provider_failure", False),
                    "done_reason": "stop",
                    "eval_count": 20,
                }
            ).encode()
        )

    args = [
        "--repository",
        "owner/repo",
        "--repository-id",
        "123",
        "--pull-request",
        "7",
        "--head-sha",
        fixture.requested_head,
        "--config",
        str(fixture.root / ".reviewsensei.yml"),
    ]
    if reply:
        args += [
            "--source-comment-id",
            "41",
            "--source-updated-at",
            fixture.source["updated_at"],
            "--source-kind",
            "issue",
            "--allow-write",
            "--enable-reply",
            "--generate",
            "--provider",
            "cloud-ollama",
            "--model",
            "deepseek-v4.1-flash:cloud",
            "--base-url",
            "https://ollama.com/api",
        ]
    else:
        args += [
            "--diff",
            str(fixture.root / "review.patch"),
            "--base-sha",
            BASE,
            "--review-mode",
            "merge-focused",
            "--no-learning-proposals",
            "--learning-root",
            str(fixture.root),
            "--configuration-context-output",
            str(fixture.root / "configuration.json"),
            "--admission-context-output",
            str(fixture.root / "admission.json"),
        ]
    argv = [
        "reply-run" if reply else "review-run",
        "--policy-commit",
        fixture.policy,
        "--run-directory",
        str(fixture.root),
    ]
    if not reply:
        argv += ["--publication-outcome", str(fixture.root / "publication.json")]
    argv += ["--", *args]
    stdout, stderr = io.StringIO(), io.StringIO()
    old = Path.cwd()
    try:
        os.chdir(fixture.root)
        with ExitStack() as stack:
            for context in (
                patch.dict(os.environ, environment, clear=True),
                patch.object(review_run, "physical_opener", fixture.external),
                patch.object(review_run, "ApiRunRequestTransport", caller),
                patch.object(review_run, "GitHubHttp", http),
                patch.object(
                    review_run,
                    "BrokerClient",
                    lambda **kw: BrokerClient(
                        broker_url="https://broker.github.test/api/github/token", **kw
                    ),
                ),
                patch(
                    "review_sensei.providers.registry.OllamaProvider",
                    lambda **kw: OllamaProvider(opener=provider_open, **kw),
                ),
                patch.object(
                    socket.socket,
                    "connect",
                    side_effect=AssertionError("live network prohibited"),
                ),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                stack.enter_context(context)
            code = cli.main(argv)
    finally:
        os.chdir(old)
        fixture.stdout, fixture.error = stdout.getvalue(), stderr.getvalue()
    return code


class ReviewFixture(EntryFixture):
    def __init__(self, directory, *, partial=False, mode="none"):
        super().__init__(directory, limit=512)
        self.clock = 0.0
        self.mode = mode
        self.hit = []
        self.requests = []
        if partial:
            self.config_bytes += b"advanced:\n  resources:\n    max_provider_calls: 1\n  review_work:\n    mode: unified\n    batch_diff_bytes: 300\n"
            commit_policy(self)
            diff = "".join(
                f"diff --git a/src/{name}.py b/src/{name}.py\n--- a/src/{name}.py\n+++ b/src/{name}.py\n@@ -1 +1 @@\n-old\n+{'x' * 130}\n"
                for name in ("app", "other")
            )
            (self.root / "review.patch").write_text(
                diff, encoding="utf-8", newline="\n"
            )

    def external(self, request, timeout):
        target = urlsplit(request.full_url)
        path, method = target.path, request.get_method()
        query = parse_qs(target.query)
        self.requests.append(
            {"method": method, "path": path, "query": query, "timeout": timeout}
        )
        listing = method == "GET" and path.endswith("/issues/7/comments")
        if listing and self.mode not in {
            "none",
            "matching",
            "mismatch",
            "wrong-author",
        }:
            mode = self.mode
            page, size = (
                int(query.get("page", ["1"])[0]),
                int(query.get("per_page", ["100"])[0]),
            )
            if mode in {"scan-limit", "oversized-page"}:
                self.hit.append("listing-page-" + str(page))
                self.trace.append((self.attempt, method, path))
                count = size + 1 if mode == "oversized-page" else size
                return json_response(
                    [
                        {
                            "id": 3000 + i,
                            "body": "unrelated synthetic history",
                            "user": {"id": 42, "type": "User", "login": "other"},
                            "issue_url": "https://api.github.test/repos/owner/repo/issues/7",
                        }
                        for i in range(count)
                    ]
                )
            if mode.startswith("second-page-") and page == 1:
                self.hit.append("listing-page-1")
                self.trace.append((self.attempt, method, path))
                return json_response(
                    [
                        {
                            "id": 3000 + i,
                            "body": "unrelated synthetic history",
                            "user": {"id": 42, "type": "User", "login": "other"},
                            "issue_url": "https://api.github.test/repos/owner/repo/issues/7",
                        }
                        for i in range(size)
                    ]
                )
            selected = mode.removeprefix("second-page-")
            if selected in {
                "403",
                "malformed",
                "wrong-type",
                "timeout",
                "shared-deadline",
            }:
                self.hit.append(selected)
                self.trace.append((self.attempt, method, path))
                if selected == "timeout":
                    raise TimeoutError("synthetic external timeout")
                if selected == "shared-deadline":
                    self.clock += timeout + 1
                    return json_response([])
                return json_response(
                    {"message": "synthetic denied or malformed"},
                    403 if selected == "403" else 200,
                )
            if mode.startswith("second-page-"):
                self.hit.append("listing-page-" + str(page))
                self.trace.append((self.attempt, method, path))
                rows = ([self.comment] if self.comment else []) + list(
                    self.extra_comments.values()
                )
                return json_response(
                    copy.deepcopy(rows[(page - 2) * size : (page - 1) * size])
                )
        return super().external(request, timeout)


class ReplyFixture(ReplyEntryFixture):
    def __init__(self, directory, *, limit=512):
        super().__init__(directory, limit=limit)
        self.clock = 0.0
        self.requests = []
        self.hit = []
        self.grant_fault = None

    def broker_response(self, payload):
        result = super().broker_response(payload)
        if self.grant_fault and payload.get("capability") == "review_session":
            self.hit.append("grant-" + self.grant_fault)
            result["session_attestation"][self.grant_fault] = (
                "review" if self.grant_fault == "purpose" else "f" * 40
            )
        return result

    def external(self, request, timeout):
        self.requests.append(
            {
                "method": request.get_method(),
                "path": urlsplit(request.full_url).path,
                "query": parse_qs(urlsplit(request.full_url).query),
                "timeout": timeout,
            }
        )
        return super().external(request, timeout)


def receipt(fixture):
    from review_sensei.session import SessionLoadError

    parse_error = None
    try:
        record = (
            parse_session_comment(fixture.comment["body"], identity=IDENTITY)
            if fixture.comment
            else None
        )
    except SessionLoadError as error:
        record = None
        parse_error = str(error)
    state = fixture.accounted._state if fixture.accounted else None
    return {
        "stderr": fixture.error,
        "root_parse_error": parse_error,
        "provider_calls": fixture.provider_calls,
        "physical_attempts": len(fixture.trace),
        "observations": sum(len(t.observations) for t in fixture.transports),
        "caller_limit": state.configuration.limit if state else None,
        "root_generation": record.generation if record else None,
        "transaction_phase": record.transaction.phase
        if record and record.transaction
        else None,
        "terminal_transaction": record.api_run_accounting.get("terminal_transaction")
        if record and record.api_run_accounting
        else None,
        "action": record.api_run_accounting.get("action")
        if record and record.api_run_accounting
        else None,
        "uncertain": state.uncertain if state else 0,
        "pending_attempts": sum(len(b.pending) for b in state.blocks) if state else 0,
        "conserved": state.startup.liability
        + state.prior_nonstartup_liability
        + state.confirmed
        + state.uncertain
        + state.reserved_undispatched
        + state.remaining
        == state.configuration.limit
        if state
        else None,
        "reviews": [
            {"state": r["state"], "commit_id": r["commit_id"]} for r in fixture.reviews
        ],
        "check_conclusions": [x.get("conclusion") for x in fixture.check_runs],
        "requests": getattr(fixture, "requests", []),
    }
