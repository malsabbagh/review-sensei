"""Run the installed console entry point in isolated, socket-denied processes.

Only external GitHub/broker/provider ports are fake. Product code is loaded from
the candidate wheel, and every packaged product file is checked against that
wheel. No live credentials, network, provider calls or GitHub writes are used.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def child(case: str, report: Path, wheel: Path) -> None:
    # Test fixtures are repository data; never place src on the import path.
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root))
    import review_sensei
    from review_sensei import cli
    from review_sensei.hosting import github
    from review_sensei.human_assessment import MAX_HUMAN_SOURCE_BYTES
    from tests.test_human_assessment import (
        APP,
        HEAD,
        HUMAN,
        Broker,
        Provider,
        State,
        prior,
        response_for,
    )
    from tests.test_reply_ingress_pending import state_with_inventory

    package = Path(review_sensei.__file__).resolve().parent
    assert "site-packages" in package.parts and package != root / "src/review_sensei"
    checked = 0
    with zipfile.ZipFile(wheel) as archive:
        for name in archive.namelist():
            if name.startswith("review_sensei/") and not name.endswith("/"):
                assert archive.read(name) == (package.parent / name).read_bytes(), name
                checked += 1

    source_bytes = None
    if case.startswith("ingress") or case == "escaped":
        state = State()
        size = int(case.split("-")[1]) if case != "escaped" else 32768
        extra = size - len(HUMAN.encode())
        suffix = "x" * extra
        if case.endswith("utf8"):
            suffix = "😀" * (extra // 4) + "x" * (extra % 4)
        if case == "escaped":
            suffix = "\\" * extra
        state.source["body"] = HUMAN + suffix
        source_bytes = len(state.source["body"].encode())
        provider = Provider(response_for(state.eligibility))
        generate = True
    else:
        state = state_with_inventory()
        provider = Provider({})
        generate = False
        if case == "authoritative-zero":
            state = State(
                replace(
                    prior(),
                    human_review=None,
                    facts=replace(
                        prior().facts,
                        has_human_adjudication_findings=False,
                        has_blocking_findings=False,
                        review_status="partial",
                        coverage_blocker="partial",
                    ),
                )
            )
            state.files = []
        if case == "stale":

            def hook(value, method, _path, _body):
                if method == "GET" and value.reply_count:
                    value.head = "c" * 40

            state.hook = hook
        if case == "edited-source":

            def hook(value, method, _path, _body):
                if method == "GET" and value.reply_count:
                    value.source["updated_at"] = "edited"

            state.hook = hook
        if case == "readback-conflict":

            def hook(value, method, path, _body):
                if (
                    method == "GET"
                    and path.endswith("/issues/1/comments")
                    and value.reply_count
                ):
                    value.replies[0]["body"] = "CHANGED " + value.replies[0]["body"]

            state.hook = hook
    previous_authority = state.reviews[0]["body"]
    previous_authority_snapshot = json.dumps(state.reviews, sort_keys=True)
    network_attempts: list[str] = []

    def deny(*_args, **_kwargs):
        network_attempts.append("socket")
        raise AssertionError("real sockets are forbidden")

    entry = next(
        item
        for item in importlib.metadata.entry_points(group="console_scripts")
        if item.name == "review-sensei"
    ).load()
    runs = []
    with tempfile.TemporaryDirectory() as directory:
        reply = Path(directory) / "reply.json"
        reply.write_text(
            json.dumps(
                {
                    "body": "RS-F7A776 remains pending according to my discussion."
                    if case == "authoritative-zero"
                    else "Discussion.",
                    "resolve": False,
                }
            )
        )
        arguments = [
            "github",
            "reply",
            "--allow-write",
            "--enable-reply",
            "--repository",
            "owner/repo",
            "--pull-request",
            "1",
            "--source-comment-id",
            "10",
            "--source-updated-at",
            "updated",
            "--head-sha",
            HEAD,
            "--source-kind",
            "issue",
            "--root-comment-id",
            "10",
            "--app-slug",
            APP,
            "--oidc-token",
            "synthetic-oidc",
        ]
        if generate:
            arguments += [
                "--generate",
                "--provider",
                "local-ollama",
                "--model",
                "fixture-model",
                "--github-token-env",
                "QUALIFICATION_READ_TOKEN",
            ]
        else:
            arguments += ["--reply", str(reply)]
        overrides = {"QUALIFICATION_READ_TOKEN": "synthetic-read"}
        registry = SimpleNamespace(create=lambda _settings: provider)
        with (
            patch.dict(os.environ, overrides),
            patch.multiple(
                github, GitHubHttp=lambda: state.http, BrokerClient=lambda: Broker()
            ),
            patch.object(cli, "default_registry", return_value=registry),
            patch.object(socket, "create_connection", deny),
            patch.object(socket.socket, "connect", deny),
            patch.object(socket.socket, "connect_ex", deny),
        ):
            for _attempt in range(2 if case == "retry" else 1):
                stdout, stderr = io.StringIO(), io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    if case == "exhausted":
                        with patch(
                            "review_sensei.hosting.github.reply_budget.MAX_REPLY_PUBLICATION_REQUESTS",
                            8,
                        ):
                            code = entry(arguments)
                    else:
                        code = entry(arguments)
                runs.append(
                    {
                        "exit": code,
                        "stdout": stdout.getvalue(),
                        "stderr": stderr.getvalue(),
                    }
                )
    assert network_attempts == []
    pending = state.eligibility.human_review
    complete_ids = pending is not None and all(
        sum(reply["body"].count(item.fingerprint) for reply in state.replies) == 1
        for item in pending.pending
    )
    if case.startswith("ingress"):
        accepted = source_bytes <= MAX_HUMAN_SOURCE_BYTES
        assert bool(provider.calls) == accepted, runs
        assert runs[0]["exit"] == (0 if accepted else 1), runs
        assert bool(state.reply_count) == accepted
        if accepted:
            prompt = provider.calls[0].prompt
            assert (
                json.loads(prompt[prompt.index("{") :])["human_reply"]
                == state.source["body"]
            )
        else:
            assert (
                json.dumps(state.reviews, sort_keys=True) == previous_authority_snapshot
            )
    elif case == "escaped":
        assert (
            not provider.calls
            and json.dumps(state.reviews, sort_keys=True) == previous_authority_snapshot
        )
    elif case in {"long", "retry"}:
        assert runs[0]["exit"] == 0 and runs[0]["stdout"].strip() == "replied"
        assert complete_ids and 1 < state.reply_count <= 16
        if case == "retry":
            assert runs[1]["stdout"].strip() == "already_replied"
    elif case in {"stale", "edited-source"}:
        assert runs[0]["stdout"].strip() == (
            "skipped_stale_head" if case == "stale" else "skipped_edited_source"
        )
        assert state.reply_count == 1 and state.events() == []
    elif case in {"readback-conflict", "exhausted"}:
        assert runs[0]["exit"] == 1 and state.events() == [], runs
    elif case == "authoritative-zero":
        authority, discussion = state.replies[0]["body"].split(
            "## Provider discussion", 1
        )
        assert "RS-F7A776" not in authority and "RS-F7A776" in discussion
        assert "No pending human-assessment findings were recorded" in authority
        assert "coverage is `partial`" in authority
        assert state.events() == []
    if case in {
        "long",
        "retry",
        "stale",
        "edited-source",
        "readback-conflict",
        "exhausted",
        "authoritative-zero",
        "escaped",
    }:
        assert json.dumps(state.reviews, sort_keys=True) == previous_authority_snapshot
    assert all(len(reply["body"].encode()) <= 16384 for reply in state.replies)
    result = {
        "case": case,
        "status": "PASS",
        "source_bytes": source_bytes,
        "max_human_source_bytes": MAX_HUMAN_SOURCE_BYTES,
        "installed_origin": str(package),
        "product_files_verified": checked,
        "provider_calls": len(provider.calls),
        "mock_requests": len(state.calls),
        "mock_reply_pages": state.reply_count,
        "all_pending_ids_delivered": bool(complete_ids),
        "original_receipt_preserved": previous_authority == state.reviews[0]["body"],
        "authority_records_changed": previous_authority_snapshot
        != json.dumps(state.reviews, sort_keys=True),
        "authority_events": state.events(),
        "review_count_before": len(json.loads(previous_authority_snapshot)),
        "review_count_after": len(state.reviews),
        "authority_digest_before": hashlib.sha256(
            previous_authority_snapshot.encode()
        ).hexdigest(),
        "authority_digest_after": hashlib.sha256(
            json.dumps(state.reviews, sort_keys=True).encode()
        ).hexdigest(),
        "sockets": len(network_attempts),
        "runs": runs,
    }
    report.write_text(json.dumps(result, indent=2) + "\n")
    raise SystemExit(runs[-1]["exit"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", type=Path)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--child")
    args = parser.parse_args()
    if args.child:
        child(args.child, args.output, args.wheel)
    args.output.mkdir(parents=True, exist_ok=False)
    cases = [
        f"ingress-{size}-{kind}"
        for size in (32767, 32768, 32769)
        for kind in ("ascii", "utf8")
    ] + [
        "escaped",
        "long",
        "retry",
        "stale",
        "edited-source",
        "readback-conflict",
        "exhausted",
        "authoritative-zero",
    ]
    results = []
    environment = {
        key: os.environ[key]
        for key in ("PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL")
        if key in os.environ
    }
    for case in cases:
        output = args.output / (case + ".json")
        process = subprocess.run(
            [
                str(args.python.absolute()),
                "-I",
                str(Path(__file__).resolve()),
                "--child",
                case,
                "--wheel",
                str(args.wheel.resolve()),
                "--output",
                str(output.resolve()),
            ],
            env=environment,
            cwd=args.output,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if not output.exists():
            raise AssertionError(f"{case}: {process.returncode}: {process.stderr}")
        result = json.loads(output.read_text())
        assert process.returncode == result["runs"][-1]["exit"]
        result["process_exit"] = process.returncode
        results.append(result)
    receipt = {
        "status": "PASS",
        "cases": len(results),
        "wheel_sha256": hashlib.sha256(args.wheel.read_bytes()).hexdigest(),
        "scope": "Installed console entry point in isolated child processes; complete wheel product bytes verified; only external ports mocked; no sockets/live providers/live GitHub writes",
        "results": results,
    }
    (args.output / "report.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(
        json.dumps({key: value for key, value in receipt.items() if key != "results"})
    )


if __name__ == "__main__":
    main()
