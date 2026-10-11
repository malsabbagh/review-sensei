#!/usr/bin/env python3
"""Exercise an installed CLI/native executable against isolated synthetic inputs.

Uses only loopback HTTP and temporary configuration. No credentials or GitHub
writes. The harness does not import the checkout's review_sensei package.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DIFF = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n"
CLEAN = '{"summary":"Synthetic review complete.","comments":[]}'
PROSE = (
    Path(__file__).resolve().parents[1]
    / "tests/fixtures/qualification/synthetic-prose-json.txt"
).read_text()


def qualify(executable: Path) -> list[dict[str, object]]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "SYSTEMROOT", "WINDIR", "TMP", "TEMP", "LANG"}
    }
    rows: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(
        prefix="reviewsensei-cli-qualification-"
    ) as temporary:
        root = Path(temporary)
        config = root / ".reviewsensei.yml"
        (root / "input.patch").write_text(DIFF)
        (root / "response.json").write_text(CLEAN)

        def run(name, arguments, expected, extra_env=None):
            result = subprocess.run(
                [str(executable), *arguments],
                cwd=root,
                env={**environment, **(extra_env or {})},
                capture_output=True,
                text=True,
                timeout=20,
            )
            assert result.returncode == expected, (
                name,
                result.returncode,
                result.stderr,
            )
            assert "Traceback" not in result.stderr, name
            rows.append({"case": name, "exit": result.returncode, "status": "PASS"})
            return result

        version = run("version", ["--version"], 0).stdout.strip()
        assert (
            run(
                "helper-version", ["resolve-hosted-openrouter", "--version"], 0
            ).stdout.strip()
            == version
        )
        run("helper-action-required", ["resolve-hosted-openrouter"], 2)
        run("helper-normal-action", ["resolve-hosted-openrouter", "both"], 0)
        for command, expected in (("doctor", 2), ("plan", 3)):
            for name, overrides, flags, backend in (
                ("file", {}, [], "cloud-ollama"),
                (
                    "environment",
                    {"REVIEWSENSEI_PROVIDER": "local-ollama"},
                    [],
                    "local-ollama",
                ),
                (
                    "cli",
                    {"REVIEWSENSEI_PROVIDER": "local-ollama"},
                    ["--provider", "cloud-ollama"],
                    "cloud-ollama",
                ),
                (
                    "retired",
                    {"REVIEWSENSEI_PROVIDER_MODE": "local", "OLLAMA_MODEL": "retired"},
                    [],
                    "cloud-ollama",
                ),
            ):
                config.write_text("schema: 1\ninference:\n  backend: cloud-ollama\n")
                before = {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}
                result = run(
                    command + "-" + name,
                    [command, "--config", str(config), "--json", *flags],
                    expected,
                    overrides,
                )
                actual = json.loads(result.stdout)["provider_configuration"]
                assert actual["backend"] == backend
                assert actual["credential_required"] == (backend == "cloud-ollama")
                assert actual["base_url"] == (
                    "https://ollama.com/api"
                    if backend == "cloud-ollama"
                    else "http://127.0.0.1:11434/api"
                )
                assert {
                    p.name: p.read_bytes() for p in root.iterdir() if p.is_file()
                } == before
            for label, text in (
                ("schema", "schema: 2\n"),
                (
                    "resource",
                    "schema: 1\nadvanced:\n  resources:\n    max_provider_calls: 33\n",
                ),
            ):
                config.write_text(text)
                run(command + "-invalid-" + label, [command, "--json"], 2)

        config.write_text("schema: 1\n")
        review_args = [
            "--diff",
            "input.patch",
            "--provider",
            "fixture",
            "--fixture-response",
            "response.json",
            "--format",
            "json",
        ]
        for value in ("nan", "inf", "-inf", "1e999", "0", "-1"):
            run(
                "timeout-invalid-" + value,
                [*review_args, "--timeout-seconds=" + value],
                2,
            )
        for value in ("0.5", "1", "3600", "3601"):
            run("timeout-valid-" + value, [*review_args, "--timeout-seconds", value], 0)

        state = {"calls": 0, "responses": [], "hold": False}
        arrived, release = threading.Event(), threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                assert self.path == "/api/generate"
                assert "Authorization" not in self.headers
                payload = json.loads(
                    self.rfile.read(int(self.headers["Content-Length"]))
                )
                assert payload["format"] == "json" and payload["think"] is False
                state["calls"] += 1
                if state["hold"]:
                    arrived.set()
                    release.wait(15)
                    text = CLEAN
                else:
                    text = state["responses"].pop(0)
                document = json.dumps(
                    {
                        "response": text,
                        "model": "synthetic-model",
                        "done": True,
                        "done_reason": "stop",
                        "eval_count": 12,
                    }
                ).encode()
                try:
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(document)))
                    self.end_headers()
                    self.wfile.write(document)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        args = [
            "--diff",
            "input.patch",
            "--provider",
            "local-ollama",
            "--base-url",
            f"http://127.0.0.1:{server.server_port}/api",
            "--model",
            "synthetic-model",
            "--format",
            "json",
            "--no-learning-proposals",
        ]
        try:
            for name, responses, cap, exit_code, complete in (
                ("prose-repeated", [PROSE, PROSE], 8, 2, False),
                ("prose-corrected", [PROSE, CLEAN], 8, 0, True),
                ("prose-call-cap", [PROSE], 1, 2, False),
            ):
                state.update(calls=0, responses=responses.copy(), hold=False)
                config.write_text(
                    f"schema: 1\nadvanced:\n  review_work:\n    mode: unified\n  resources:\n    max_provider_calls: {cap}\n"
                )
                result = run(name, args, exit_code)
                assert state["calls"] == len(responses)
                assert (
                    json.loads(result.stdout)["coverage"]["fully_reviewed"] is complete
                )
                rows[-1]["provider_calls"] = state["calls"]
            if os.name != "nt":
                config.write_text(
                    "schema: 1\nadvanced:\n  review_work:\n    mode: unified\n"
                )
                state.update(calls=0, hold=True)
                child = subprocess.Popen(
                    [str(executable), *args],
                    cwd=root,
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                try:
                    assert arrived.wait(10), (
                        "provider dispatch never reached cancellation seam"
                    )
                    child.send_signal(signal.SIGINT)
                    stdout, stderr = child.communicate(timeout=10)
                    assert child.returncode == 130, (child.returncode, stderr)
                    assert not stdout
                    assert stderr == "review-sensei: cancelled\n", stderr
                    assert state["calls"] == 1
                    rows.append(
                        {
                            "case": "sigint-pending-provider",
                            "exit": 130,
                            "provider_calls": 1,
                            "status": "PASS",
                        }
                    )
                finally:
                    release.set()
                    if child.poll() is None:
                        child.kill()
                        child.communicate()
            else:
                rows.append(
                    {
                        "case": "sigint-pending-provider",
                        "status": "SKIPPED",
                        "reason": "POSIX signal test",
                    }
                )
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", required=True, type=Path)
    args = parser.parse_args()
    print(
        json.dumps(
            {
                "executable": str(args.executable.resolve()),
                "cases": qualify(args.executable.resolve()),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
