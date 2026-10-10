"""Real killed children replay public host seams against synthetic remote state.

The parent kills a blocked child only after its durable checkpoint is visible.
The synthetic API's state outlives that child, modeling a committed write whose
HTTP response was lost. No network, live provider, or production approval runs.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import nullcontext
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse

from review_sensei.budgets import ReviewWorkBudgets
from review_sensei.hosting.github.errors import GitHubConversationError
from review_sensei.hosting.github.publication import approval_eligibility_from_body
from review_sensei.work_recovery import WorkRecoveryStore
from tests import test_human_assessment as host
from tests import test_work_recovery as recovery
from tests.fake_github_http import json_response
from tests.test_review_work import AssessingProvider

CHECKPOINT_TIMEOUT = 20
ROOT = Path(__file__).resolve().parents[1]


def write_json(path, value):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def checkpoint(directory, boundary):
    write_json(directory / "checkpoint.json", {"boundary": boundary})
    # Parent owns termination; EOF is an error, not permission to continue.
    sys.stdin.buffer.read(1)
    raise RuntimeError("fault checkpoint was released without termination")


class DurableHost(host.State):
    def __init__(self, directory, boundary=None):
        super().__init__()
        self.directory = directory
        self.boundary = boundary
        self.provider_calls = 0
        path = directory / "remote.json"
        if path.exists():
            for key, value in json.loads(path.read_text(encoding="utf-8")).items():
                setattr(self, key, value)

    def save(self):
        write_json(
            self.directory / "remote.json",
            {
                key: getattr(self, key)
                for key in (
                    "reviews",
                    "replies",
                    "reply_count",
                    "calls",
                    "source",
                    "head",
                    "base",
                    "provider_calls",
                )
            },
        )

    def open(self, request, timeout):
        if (
            self.source is None
            and request.method == "GET"
            and urlparse(request.full_url).path.endswith("/issues/comments/10")
        ):
            return json_response({}, 404)
        response = super().open(request, timeout)
        self.save()
        path = urlparse(request.full_url).path
        body = json.loads(request.data) if request.data else None
        boundary = None
        if request.method == "POST":
            if path.endswith("/issues/1/comments"):
                boundary = "source-ack-committed"
            elif path.endswith("/pulls/1/reviews"):
                boundary = body["event"].lower() + "-committed"
        elif (
            request.method == "GET"
            and path.endswith("/pulls/1/reviews")
            and len(self.reviews) == 2
        ):
            boundary = "comment-readback"
        if boundary is not None and boundary == self.boundary:
            checkpoint(self.directory, boundary)
        return response


class DurableProvider(AssessingProvider):
    def __init__(self, state):
        super().__init__()
        self.state = state

    def complete(self, request):
        self.state.provider_calls += 1
        self.state.save()
        return super().complete(request)


def host_child(directory, boundary):
    state = DurableHost(directory, boundary)
    state.application_reply(
        DurableProvider(state), work_budgets=ReviewWorkBudgets(mode="unified")
    )
    raise RuntimeError("requested host boundary was never reached")


class FaultRecoveryStore(WorkRecoveryStore):
    def __init__(self, directory, boundary):
        super().__init__(
            directory / "receipts",
            key=b"k" * 32,
            artifacts="diagnostics",
            now=lambda: recovery.NOW,
        )
        self.signal_directory = directory
        self.boundary = boundary

    def save(self, execution, tracker, encode, request_digests):
        accepted = bool(execution.completed)
        if accepted and self.boundary == "before-accepted-receipt":
            checkpoint(self.signal_directory, self.boundary)
        super().save(execution, tracker, encode, request_digests)
        if self.boundary == "dispatch-charged" and not accepted:
            checkpoint(self.signal_directory, self.boundary)
        if accepted and self.boundary == "accepted-receipt":
            checkpoint(self.signal_directory, self.boundary)


def recovery_child(directory, boundary):
    fixture = recovery.WorkRecoveryTests()
    plan, resource, budgets, render = fixture.fixture()
    fixture.run_plan(
        plan,
        resource,
        budgets,
        render,
        recovery.Provider(),
        FaultRecoveryStore(directory, boundary),
    )
    raise RuntimeError("requested receipt boundary was never reached")


def kill_at(directory, kind, boundary):
    code = (
        "import sys; from pathlib import Path; "
        "from tests.test_epic238_process_faults import host_child, recovery_child; "
        f"{kind}_child(Path(sys.argv[1]), sys.argv[2])"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(directory), boundary],
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + CHECKPOINT_TIMEOUT
        while not (directory / "checkpoint.json").exists():
            if child.poll() is not None:
                _, errors = child.communicate(timeout=5)
                raise AssertionError(errors.decode())
            if time.monotonic() >= deadline:
                raise AssertionError(f"child did not reach {boundary}")
            time.sleep(0.01)
        assert json.loads((directory / "checkpoint.json").read_text()) == {
            "boundary": boundary
        }
        child.kill()
        child.communicate(timeout=5)
        assert child.returncode != 0
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=5)


class ProcessFaultTests(unittest.TestCase):
    def test_killed_source_ack_retains_pending_authority_before_restart(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            kill_at(directory, "host", "source-ack-committed")
            state = DurableHost(directory)
            self.assertEqual(state.events(), [])
            self.assertEqual(state.reply_count, 1)
            self.assertEqual(state.provider_calls, 1)
            self.assertEqual(
                approval_eligibility_from_body(state.reviews[0]["body"]),
                state.eligibility,
            )
            self.assertEqual(len(state.eligibility.human_review.pending), 1)

    def test_restart_revalidates_source_snapshot_and_newer_authority(self):
        for change in (
            "head",
            "base",
            "edit",
            "delete",
            "actor",
            "authorization",
            "newer",
        ):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as root:
                directory = Path(root)
                kill_at(directory, "host", "comment-committed")
                state = DurableHost(directory)
                retained_body = state.reviews[1]["body"]
                if change == "head":
                    state.head = "c" * 40
                elif change == "base":
                    state.base = "d" * 40
                elif change == "edit":
                    state.source["updated_at"] = "edited"
                    state.source["body"] = "@sensei Approve now."
                elif change == "delete":
                    state.source = None
                elif change == "actor":
                    state.source["user"] = {"login": host.APP, "type": "Bot"}
                elif change == "authorization":
                    state.source["author_association"] = "NONE"
                else:
                    # A new same-head human review must beat the older resolved one.
                    state.reviews.append(state.review(state.eligibility, 100))
                    state.source["body"] = "@sensei Approve now."
                refusal = (
                    self.assertRaisesRegex(
                        GitHubConversationError, "exact reviewed base/head"
                    )
                    if change == "base"
                    else nullcontext()
                )
                with refusal:
                    state.application_reply(
                        DurableProvider(state),
                        work_budgets=ReviewWorkBudgets(mode="unified"),
                    )
                self.assertNotIn("APPROVE", state.events())
                self.assertEqual(state.reviews[1]["body"], retained_body)
                self.assertEqual(state.reply_count, 1)

    def test_killed_dispatch_and_receipt_boundaries_preserve_original_allowance(self):
        for boundary, completed_before in (
            ("dispatch-charged", 0),
            ("before-accepted-receipt", 0),
            ("accepted-receipt", 1),
        ):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as root:
                directory = Path(root)
                kill_at(directory, "recovery", boundary)
                receipt = next((directory / "receipts").glob("*.json"))
                before = json.loads(receipt.read_text())["document"]
                self.assertEqual(before["counters"]["provider_calls"], 1)
                self.assertEqual(len(before["completed"]), completed_before)
                fixture = recovery.WorkRecoveryTests()
                plan, resource, budgets, render = fixture.fixture()
                provider = recovery.Provider()
                resumed, tracker = fixture.run_plan(
                    plan,
                    resource,
                    budgets,
                    render,
                    provider,
                    WorkRecoveryStore(
                        directory / "receipts",
                        key=b"k" * 32,
                        artifacts="diagnostics",
                        now=lambda: recovery.NOW + timedelta(seconds=30),
                    ),
                )
                after = json.loads(receipt.read_text())["document"]
                self.assertEqual(after["expires_at"], before["expires_at"])
                self.assertEqual(
                    after["execution_identity"], before["execution_identity"]
                )
                self.assertEqual(after["request_digests"], before["request_digests"])
                self.assertEqual(tracker.provider_calls, 2)
                self.assertEqual(provider.calls, 1)
                self.assertGreaterEqual(tracker.elapsed_ms(), 30000)
                self.assertEqual(len(resumed.completed), 1 + completed_before)
                self.assertEqual(len(resumed.pending), 1 - completed_before)
                # Replay itself cannot grant a third call or change retained expiry.
                replay, replay_tracker = fixture.run_plan(
                    plan,
                    resource,
                    budgets,
                    render,
                    provider,
                    WorkRecoveryStore(
                        directory / "receipts",
                        key=b"k" * 32,
                        artifacts="diagnostics",
                        now=lambda: recovery.NOW + timedelta(seconds=40),
                    ),
                )
                self.assertEqual(provider.calls, 1)
                self.assertEqual(replay.completed, resumed.completed)
                self.assertEqual(replay_tracker.provider_calls, 2)

    def test_committed_comment_readback_and_approve_survive_actual_kill(self):
        for boundary in ("comment-committed", "comment-readback", "approve-committed"):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as root:
                directory = Path(root)
                kill_at(directory, "host", boundary)
                state = DurableHost(directory)
                original_body = state.reviews[1]["body"]
                original_reply = state.replies[0]["body"]
                self.assertEqual(state.provider_calls, 1)
                self.assertEqual(state.reply_count, 1)
                self.assertFalse(
                    approval_eligibility_from_body(original_body).human_review.pending
                )
                for _ in range(2):
                    outcome, _ = state.application_reply(
                        DurableProvider(state),
                        work_budgets=ReviewWorkBudgets(mode="unified"),
                    )
                    self.assertIn(
                        outcome.approval_status, ("approved", "already_approved")
                    )
                self.assertEqual(state.events(), ["COMMENT", "APPROVE"])
                self.assertEqual(state.provider_calls, 1)
                self.assertEqual(state.reply_count, 1)
                self.assertEqual(state.reviews[1]["body"], original_body)
                self.assertEqual(state.replies[0]["body"], original_reply)


if __name__ == "__main__":
    unittest.main()
