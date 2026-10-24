#!/usr/bin/env python3
"""Installed CLI against a loopback synthetic Ollama wire transport."""

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from qualify import CLEAN, PATCH, Runner


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--expected-version", required=True)
    p.add_argument("--report", type=Path, required=True)
    p.add_argument("--native-executable", type=Path)
    a = p.parse_args()
    a.case_timeout = 15
    import importlib.metadata

    assert importlib.metadata.version("review-sensei") == a.expected_version
    runner = Runner(a)
    rows = []
    state = {"responses": [], "requests": [], "delay": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append(
                {
                    "path": self.path,
                    "prompt_bytes": len(request.get("prompt", "").encode()),
                    "model": request.get("model"),
                    "options": request.get("options"),
                    "json_mode": request.get("format"),
                    "authorization_present": "Authorization" in self.headers,
                }
            )
            time.sleep(state["delay"])
            idx = min(len(state["requests"]) - 1, len(state["responses"]) - 1)
            item = state["responses"][idx]
            status = item.get("http", 200)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Retry-After", "0")
            self.end_headers()
            envelope = {
                "response": item.get("text", json.dumps(CLEAN)),
                "done": True,
                "done_reason": "stop",
                "eval_count": 10,
                "model": request["model"],
            }
            envelope.update(item.get("envelope", {}))
            try:
                self.wfile.write(json.dumps(envelope).encode())
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_port}/api"
    try:
        specs = [
            ("clean", [{}], 0, 1, {}),
            ("structural-correction", [{"text": "{"}, {}], 0, 2, {}),
            ("structural-exhaustion", [{"text": "{"}], 2, 2, {}),
            ("transient-then-clean", [{"http": 429}, {}], 0, 2, {}),
            ("transient-exhaustion", [{"http": 503}], 2, 3, {}),
            ("nontransient", [{"http": 400}], 2, 1, {}),
            ("incomplete-envelope", [{"envelope": {"done": False}}], 2, 1, {}),
            ("truncated-envelope", [{"envelope": {"done_reason": "length"}}], 2, 1, {}),
            ("negative-tokens", [{"envelope": {"eval_count": -1}}], 2, 1, {}),
            (
                "unified-call-cap-one",
                [{"http": 503}],
                2,
                1,
                {
                    "advanced.review_work.mode": "unified",
                    "advanced.resources.max_provider_calls": 1,
                },
            ),
            (
                "unified-call-cap-two",
                [{"http": 503}],
                2,
                1,
                {
                    "advanced.review_work.mode": "unified",
                    "advanced.resources.max_provider_calls": 2,
                },
            ),
            (
                "legacy-call-cap-one-is-inactive",
                [{"http": 503}],
                2,
                3,
                {"advanced.resources.max_provider_calls": 1},
            ),
        ]
        for name, responses, exit, calls, fields in specs:
            state.update(responses=responses, requests=[], delay=0)
            argv = [
                "--diff",
                "input.patch",
                "--provider",
                "local-ollama",
                "--base-url",
                endpoint,
                "--allow-custom-endpoint",
                "--model",
                "synthetic-v1",
                "--no-learning-proposals",
                "--config",
                "config.yml",
                "--format",
                "json",
                "--outcome",
                "outcome.json",
            ]
            row = runner.run(
                {
                    "id": name,
                    "group": "transport",
                    "argv": argv,
                    "expected": exit,
                    "fields": fields,
                }
            )
            row["wire_requests"] = list(state["requests"])
            row["expected_wire_calls"] = calls
            row["checks"]["wire_calls"] = len(state["requests"]) == calls
            row["checks"]["no_credentials"] = all(
                not r["authorization_present"] for r in state["requests"]
            )
            row["level"] = "installed-CLI-loopback-fake-provider"
            row["status"] = "PASS" if all(row["checks"].values()) else "FAIL"
            rows.append(row)
        # Numeric extremes require validation evidence, never a long call.
        for value in ["nan", "inf", "3601", "1e100"]:
            state.update(responses=[{}], requests=[], delay=0)
            argv = [
                "--diff",
                "input.patch",
                "--provider",
                "local-ollama",
                "--base-url",
                endpoint,
                "--allow-custom-endpoint",
                "--model",
                "synthetic-v1",
                "--timeout-seconds",
                value,
                "--no-learning-proposals",
                "--format",
                "json",
                "--outcome",
                "outcome.json",
            ]
            row = runner.run(
                {
                    "id": "timeout-" + value,
                    "group": "transport-timeout",
                    "argv": argv,
                    "expected": 2 if value in ["nan", "inf", "1e100"] else 0,
                }
            )
            row["wire_requests"] = list(state["requests"])
            row["checks"]["invalid_timeout_preflight"] = (
                len(state["requests"]) == 0 if value in ["nan", "inf"] else True
            )
            row["level"] = "installed-CLI-loopback-fake-provider"
            row["status"] = "PASS" if all(row["checks"].values()) else "FAIL"
            rows.append(row)
        # Short real socket timeout with an independently bounded process deadline.
        state.update(responses=[{}], requests=[], delay=0.2)
        argv = [
            "--diff",
            "input.patch",
            "--provider",
            "local-ollama",
            "--base-url",
            endpoint,
            "--allow-custom-endpoint",
            "--model",
            "synthetic-v1",
            "--timeout-seconds",
            "0.03",
            "--no-learning-proposals",
            "--config",
            "config.yml",
            "--format",
            "json",
            "--outcome",
            "outcome.json",
        ]
        row = runner.run(
            {
                "id": "socket-timeout",
                "group": "transport-timeout",
                "argv": argv,
                "expected": 2,
                "fields": {"advanced.resources.review_deadline_seconds": 1},
            }
        )
        row["wire_requests"] = list(state["requests"])
        row["checks"]["bounded_socket_attempts"] = len(state["requests"]) <= 3
        row["level"] = "installed-CLI-loopback-fake-provider"
        row["status"] = "PASS" if all(row["checks"].values()) else "FAIL"
        rows.append(row)
        if os.name == "posix":
            executables = [("python-console", runner.cli)] + (
                [("native", a.native_executable.resolve())]
                if a.native_executable
                else []
            )
            for kind, exe in executables:
                time.sleep(
                    0.75
                )  # Drain previous synthetic socket handlers before observing dispatch.
                wd = runner.root / ("cancel-" + kind)
                wd.mkdir(exist_ok=True)
                (wd / "input.patch").write_text(PATCH, encoding="utf-8", newline="\n")
                state.update(responses=[{}], requests=[], delay=0.5)
                argv = [
                    str(exe),
                    "--diff",
                    "input.patch",
                    "--provider",
                    "local-ollama",
                    "--base-url",
                    endpoint,
                    "--allow-custom-endpoint",
                    "--model",
                    "synthetic-v1",
                    "--format",
                    "json",
                    "--outcome",
                    "outcome.json",
                ]
                proc = subprocess.Popen(
                    argv,
                    cwd=wd,
                    env={**runner.env, "HOME": str(wd)},
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                )
                deadline = time.monotonic() + 5
                while (
                    not state["requests"]
                    and proc.poll() is None
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.01)
                entered = bool(state["requests"])
                os.killpg(proc.pid, signal.SIGINT)
                try:
                    stdout, stderr = proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    stdout, stderr = proc.communicate()
                text = stderr.decode(errors="replace")
                (wd / "stderr.txt").write_text(text)
                (wd / "stdout.txt").write_bytes(stdout)
                checks = {
                    "entered_provider": entered,
                    "exit_130": proc.returncode == 130,
                    "no_traceback": "Traceback" not in text,
                    "no_pyinstaller_error": "Failed to execute script" not in text,
                }
                rows.append(
                    {
                        "id": "cancel-" + kind,
                        "status": "PASS" if all(checks.values()) else "FAIL",
                        "checks": checks,
                        "exit": proc.returncode,
                        "stderr_excerpt": text[:2000],
                        "level": "installed-native-loopback"
                        if kind == "native"
                        else "installed-CLI-loopback-fake-provider",
                        "wire_requests": list(state["requests"]),
                    }
                )
    finally:
        server.shutdown()
        server.server_close()
    result = {
        "version": a.expected_version,
        "total": len(rows),
        "pass": sum(x["status"] == "PASS" for x in rows),
        "fail": sum(x["status"] == "FAIL" for x in rows),
        "rows": rows,
        "provider": "loopback Ollama fake; no model inference",
    }
    (runner.root / "results.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
    return bool(result["fail"])


if __name__ == "__main__":
    sys.exit(main())
