"""Current reply journal and explicit retired-phase refusal, installed only."""

import io
import json
import os
import socket
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from run_fixture import HEAD, ReplyFixture, cli, commit_policy, invoke, receipt

spec = json.loads(sys.argv[1])
if spec["purpose"] == "retired":
    calls = []

    def deny(*args, **kwargs):
        calls.append("external")
        raise AssertionError("retired phase attempted external I/O")

    command = spec["command"]
    argv = [command]
    if command == "publish":
        argv += [
            "--result",
            "absent.json",
            "--diff",
            "absent.patch",
            "--repository",
            "owner/repo",
            "--repository-id",
            "123",
            "--pull-request",
            "7",
            "--head-sha",
            HEAD,
            "--allow-write",
        ]
    elif command == "reply":
        argv += [
            "--generate",
            "--repository",
            "owner/repo",
            "--pull-request",
            "7",
            "--source-comment-id",
            "41",
            "--source-updated-at",
            "2026-10-26T00:00:00Z",
            "--source-kind",
            "issue",
            "--allow-write",
            "--enable-reply",
        ]
    errors = io.StringIO()
    with (
        patch.dict(os.environ, {"GITHUB_RUN_ID": "456"}, clear=True),
        patch.object(socket.socket, "connect", deny),
        redirect_stderr(errors),
        redirect_stdout(io.StringIO()),
    ):
        code = cli.main(argv)
    data = {
        "exit": code,
        "stderr": errors.getvalue(),
        "completed": False,
        "physical_attempts": len(calls),
        "observations": 0,
        "provider_calls": 0,
        "reviews": [],
        "requests": [],
        "action": None,
        "terminal_transaction": None,
        "fault_hits": [],
        "live_network": "blocked",
        "level": "installed-retired-phase-refusal",
    }
else:
    f = ReplyFixture(Path.cwd(), limit=spec.get("limit", 512))
    fault = spec.get("fault")
    if fault in {"purpose", "head_sha"}:
        f.grant_fault = fault
    elif fault == "overlap":
        f.overlap = True
    elif fault == "source-author":
        f.source["user"]["type"] = "Bot"
    elif fault == "provider-incomplete":
        f.provider_failure = True
    elif fault == "lost-reply":
        f.lost_reply = "absent"
    elif fault == "cleanup":
        f.cleanup_failure = True
    elif fault == "disabled":
        f.config_bytes = f.config_bytes.replace(
            b"  writes: true", b"  operation_entry: disabled\n  writes: true"
        )
        commit_policy(f)
    code = invoke(f, reply=True, token=spec["token"] == "present", appid=spec["appid"])
    data = receipt(f)
    data.update(
        exit=code,
        completed=bool(data["action"] and data["action"]["phase"] == "terminal"),
        fault_hits=f.hit,
        reply_posts=f.reply_posts,
        live_network="blocked",
        level="installed-current-reply-run/external-HTTP-only",
    )
    if fault in {"lost-reply", "cleanup", "provider-incomplete"}:
        before = (f.provider_calls, f.reply_posts)
        f.attempt = 2
        second = invoke(f, reply=True)
        after = receipt(f)
        data.update(
            successor_exit=second,
            no_replay=(f.provider_calls, f.reply_posts) == before,
            retained_pending=after["action"] is not None
            and after["action"]["phase"] == "pending",
            successor_observations_match=after["physical_attempts"]
            == after["observations"],
        )
print(json.dumps(data))
sys.exit(code)
