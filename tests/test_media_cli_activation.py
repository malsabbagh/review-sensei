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
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

from review_sensei_github.broker_client import BrokerClient
from review_sensei_github.checks import REVIEW_CHECK_NAME
from review_sensei_github.cli import main as github_main
from review_sensei_github.human_file_review import (
    RECEIPT_GROUP_PREFIX,
    RECEIPT_PREFIX,
    REQUEST_GROUP_PREFIX,
    REQUEST_PREFIX,
)
from review_sensei_github.operation_host import RESULT_COMMENT_MARKER

from tests.fake_github_http import json_response
from tests.test_github_human_file_review import ALICE, BOT, HEAD, REPO
from tests.test_hosted_media_context import BODY, HostedMediaState

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
        self.fail_check = False
        self.lose_review_response = False
        super().__init__(**kwargs)

    def open(self, request, timeout):
        path = urlsplit(request.full_url).path
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
        self.requests.append(payload["capability"])
        control = payload["control_budget"]
        self.admitted.append(control["max_dispatches"] + 1)
        result = {
            "token": "synthetic-scoped",
            "capability": payload["capability"],
            "control_receipt": control | {"dispatched": control["max_dispatches"]},
        }
        self.receipts.append(result["control_receipt"])
        if payload["capability"] == "review_session":
            state = self.owner.state
            source = state.comments.get(20)
            if state.head != HEAD or source is None:
                raise HTTPError(
                    request.full_url, 403, "synthetic refusal", {}, io.BytesIO(b"{}")
                )
            result.update(
                session_state="known",
                session_grant=f"sg1.{int(time.time() * 1000)}." + "A" * 43,
                session_attestation=payload["session_attestation"]
                | {
                    "actor": source["user"]["login"],
                    "actor_type": source["user"]["type"],
                    "association": source["author_association"],
                    "command_id": 20,
                    "command_digest": hashlib.sha256(
                        source["body"].encode()
                    ).hexdigest(),
                },
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
        reply = self.call("oidc")
        assert reply["status"] == 200
        return reply["result"]

    def cold_invocation(self):
        reply = self.call("reset_oidc_cache")
        assert reply["status"] == 200

    def open(self, request, timeout):
        assert 0 < timeout <= 60
        payload = json.loads(request.data)
        self.requests.append(payload["capability"])
        self.admitted.append(payload["control_budget"]["max_dispatches"] + 1)
        self.attempts.append(
            {
                "origin": "caller-broker",
                "method": request.method,
                "path": urlsplit(request.full_url).path,
            }
        )
        reply = self.call(
            "exchange", body=payload, authority=self.owner.state.broker_authority()
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
        import review_sensei_github as github

        def broker_factory(**kwargs):
            control = kwargs.get("control")
            self.budgets.append(control.budget)
            self.budget_starts.append(control.budget.snapshot())
            return BrokerClient(
                broker_url="https://broker.github.test/api/github/token",
                opener=self.broker.open,
                **kwargs,
            )

        attestation = {
            "version": 1,
            "repository": "owner/repo",
            "repository_id": 1,
            "pull_request": 2,
            "head_sha": HEAD,
            "operation": "command",
            "source_comment_id": 20,
            "run_id": "123",
            "issued_at": int(time.time()),
            "concurrency_group": "reviewsensei-session-1-2",
            "job_workflow_ref": WORKFLOW,
            "job_workflow_sha": "a" * 40,
        }
        output, errors = io.StringIO(), io.StringIO()
        start_http = len(self.state.physical)
        start_admitted = len(self.broker.admitted)
        start_trace = len(getattr(self.broker, "attempts", ()))
        oidc = self.broker.oidc()
        with tempfile.TemporaryDirectory() as scratch:
            attestation_path = Path(scratch) / "attestation.json"
            attestation_path.write_text(json.dumps(attestation), encoding="utf-8")
            config_path = Path(scratch) / "configuration.yml"
            config_path.write_text(
                f"schema: 1\ngithub:\n  reviews: {policy}\n", encoding="utf-8"
            )
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
                patch.object(github, "BrokerClient", side_effect=broker_factory),
                redirect_stdout(output),
                redirect_stderr(errors),
            ):
                status = github_main(
                    [
                        "command",
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
                        "--session-attestation",
                        str(attestation_path),
                        "--oidc-token",
                        oidc,
                        "--app-slug",
                        BOT["login"],
                        "--config",
                        str(config_path),
                        "--github-session-ledger",
                        "--allow-write",
                    ]
                )
        self.last_output = output.getvalue() + errors.getvalue()
        self.last_http = len(self.state.physical) - start_http
        self.last_admitted = sum(self.broker.admitted[start_admitted:])
        self.last_physical = (
            self.last_http + len(getattr(self.broker, "attempts", ())) - start_trace
        )
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
        budget = self.budgets[-1]
        self.assertLessEqual(budget.calls, 64)
        self.assertEqual(budget.calls, self.last_http + self.last_admitted)
        self.assertEqual(
            self.budget_starts[-1]["deadline_unix_ms"],
            budget.snapshot()["deadline_unix_ms"],
        )
        self.assertTrue(
            all(
                receipt["deadline_unix_ms"] == budget.wall_deadline_ms
                for receipt in self.broker.receipts[-2:]
            )
        )
        self.assertEqual(self.broker.requests[-2:], ["review_session", "check_publish"])
        self.assertEqual(
            self.broker.receipts[-2]["operation_id"],
            self.broker.receipts[-1]["operation_id"],
        )
        if self.production_broker:
            self.assertLessEqual(self.last_physical, budget.calls)
        self.assertIn("media-approved", self.last_output)

    def retain_media_receipt_before_approval(self):
        self.broker.cold_invocation()
        self.assertEqual(self.activate(), 1, self.last_output)
        self.assertEqual(self.approvals(), [])
        self.assertEqual(self.receipt_count(), 1)
        self.assertEqual(self.state.check_runs[0]["conclusion"], "action_required")
        self.assertLessEqual(self.budgets[-1].calls, 60)
        self.assertEqual(self.budgets[-1].calls, self.last_http + self.last_admitted)
        if self.production_broker:
            self.assertLessEqual(self.last_physical, self.budgets[-1].calls)

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
                self.retain_media_receipt_before_approval()
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
                    self.assertEqual(len(self.state.tree_requests), 8)
                    self.assertEqual(
                        sorted(recursive for _, recursive in self.state.tree_requests),
                        [False] * 4 + [True] * 4,
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
                        self.assertLessEqual(self.last_physical, self.budgets[-1].calls)

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
                self.assertFalse(
                    any(
                        method != "GET" and path != "/graphql"
                        for method, path, _, _ in self.state.physical
                    )
                )
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
                self.assertLessEqual(self.budgets[-1].calls, 60)
                self.assertEqual(
                    self.budgets[-1].calls, self.last_http + self.last_admitted
                )
                if self.production_broker:
                    self.assertLessEqual(self.last_physical, self.budgets[-1].calls)

    def test_explicit_retry_is_idempotent(self):
        self.retain_media_receipt_before_approval()
        self.assertEqual(self.activate(), 0, self.last_output)
        self.assert_success()
        self.assertEqual(self.activate(), 0, self.last_output)
        self.assert_success()

    def test_lost_native_write_response_is_failure_and_explicit_retry_reconciles(self):
        self.retain_media_receipt_before_approval()
        self.state.lose_review_response = True
        self.assertEqual(self.activate(), 1, self.last_output)
        self.assertEqual(len(self.approvals()), 1)
        self.assertEqual(self.state.check_runs[0]["conclusion"], "action_required")
        self.assertEqual(self.activate(), 0, self.last_output)
        self.assert_success()
        self.assertEqual(
            sum(
                method == "POST" and path == REPO + "/pulls/2/reviews"
                for method, path, _, _ in self.state.physical
            ),
            1,
        )

    def test_failed_check_returns_failure_and_retry_preserves_the_native_approval(self):
        self.retain_media_receipt_before_approval()
        self.state.fail_check = True
        self.assertEqual(self.activate(), 1, self.last_output)
        self.assertEqual(len(self.approvals()), 1)
        self.assertEqual(self.state.check_runs[0]["conclusion"], "action_required")
        self.state.fail_check = False
        self.assertEqual(self.activate(), 0, self.last_output)
        self.assert_success()

    def test_current_maintainer_and_head_binding_refuse_before_media_writes(self):
        for field, value in (("permission", "write"), ("head", "c" * 40)):
            with self.subTest(field=field):
                self.state = MediaCLIState()
                setattr(self.state, field, value)
                self.assertEqual(self.activate(), 1, self.last_output)
                self.assertEqual(self.approvals(), [])
                self.assertEqual(self.receipt_count(), 0)
                self.assertFalse(
                    any(
                        method != "GET" and path != "/graphql"
                        for method, path, _, _ in self.state.physical
                    )
                )

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
                self.assertFalse(
                    any(
                        method != "GET" and path != "/graphql"
                        for method, path, _, _ in self.state.physical
                    )
                )

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
        self.assertFalse(
            any(
                method != "GET" and path != "/graphql"
                for method, path, _, _ in self.state.physical
            )
        )

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
        self.assertEqual(self.activate(), 1, self.last_output)
        self.assertIn("receipt is retained", self.last_output)
        self.assertEqual(self.receipt_count(), 1)
        self.assertEqual(self.approvals(), [])
        self.assertLessEqual(self.budgets[-1].calls, 60)
        self.broker.cold_invocation()
        self.assertEqual(self.activate(), 0, self.last_output)
        self.assert_success()
        checks = [
            receipt
            for receipt in self.broker.receipts
            if receipt["max_dispatches"] == 8
        ]
        self.assertEqual(len(checks), 2)
        self.assertTrue(all(receipt["dispatched"] == 8 for receipt in checks))
        self.assertEqual(
            sum(
                value["path"].endswith("/.well-known/jwks")
                for value in self.broker.attempts
            ),
            4,
        )


if __name__ == "__main__":
    unittest.main()
