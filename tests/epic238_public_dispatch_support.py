"""Measurement and IPC fixtures, never host persistence or budget authority.

Requires Node >=22.15 and the pinned Worker npm dependencies. The bridge runs
production broker policy and actual SQLite; only external transports are fake.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlparse

from review_sensei.hosting.github.broker_client import BrokerClient
from tests.fake_github_http import json_response

ROOT = Path(__file__).resolve().parents[1]


class PhysicalDispatchTrace:
    """Count attempts before transport, retaining failed/ambiguous dispatches.

    This trace measures physical requests. It never charges, refunds, restores
    or supplies an EvidenceReadBudget; production A/D owns those operations.
    Headers, source text and provider output are excluded from the trace.
    """

    def __init__(self, directory: Path):
        self.path = directory / "dispatches.jsonl"
        self.lock = threading.Lock()

    def append(self, item):
        with self.lock, self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(item, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def opener(self, transport, *, origin="host"):
        def opened(request, timeout):
            path = urlparse(request.full_url).path
            self.append(
                {
                    "phase": "attempt",
                    "origin": origin,
                    "method": request.method,
                    "path": path,
                }
            )
            try:
                response = transport(request, timeout)
            except Exception as exc:
                self.append(
                    {
                        "phase": "failed",
                        "origin": origin,
                        "method": request.method,
                        "path": path,
                        "error_type": type(exc).__name__,
                    }
                )
                raise
            self.append(
                {
                    "phase": "returned",
                    "origin": origin,
                    "method": request.method,
                    "path": path,
                }
            )
            return response

        return opened

    def attempts(self):
        if not self.path.exists():
            return ()
        records = (json.loads(line) for line in self.path.read_text().splitlines())
        return tuple(item for item in records if item["phase"] == "attempt")


class ProductionBrokerBridge:
    """Finite real broker authority in a separate process, using test inputs."""

    def __init__(self, directory: Path, trace: PhysicalDispatchTrace):
        self.directory, self.trace = directory, trace
        self.sequence = 0
        self.oidc_overrides = {}
        self.results = queue.Queue()
        self.process = subprocess.Popen(
            [
                "node",
                str(ROOT / "tests/epic238_consuming_broker_bridge.mjs"),
                str(directory / "broker.sqlite"),
                str(trace.path),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=ROOT,
            env={
                key: value
                for key, value in os.environ.items()
                if not key.startswith(
                    (
                        "REVIEWSENSEI_",
                        "OLLAMA_",
                        "OPENAI_",
                        "ANTHROPIC_",
                        "GITHUB_",
                        "GH_",
                        "ACTIONS_",
                        "AWS_",
                        "AZURE_",
                        "OPENROUTER_",
                    )
                )
                and not key.endswith(("_TOKEN", "_API_KEY", "_SECRET"))
            },
        )

        def read():
            assert self.process.stdout is not None
            for line in self.process.stdout:
                self.results.put(json.loads(line))
            self.results.put(None)

        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()

    def call(self, action, **arguments):
        self.sequence += 1
        assert self.process.stdin is not None
        self.process.stdin.write(
            json.dumps({"id": self.sequence, "action": action, **arguments}) + "\n"
        )
        self.process.stdin.flush()
        result = self.results.get(timeout=20)
        if result is None:
            raise RuntimeError("production broker bridge exited before its reply")
        if result["id"] != self.sequence:
            raise RuntimeError("production broker bridge reply identity changed")
        return result

    def open(self, request, timeout):
        assert timeout > 0
        path = urlparse(request.full_url).path
        if path == "/fixture-oidc":
            reply = self.call("oidc", overrides=self.oidc_overrides)
            return json_response({"value": reply["result"]})
        if request.method != "POST":
            raise AssertionError("broker fixture accepts only the actual POST protocol")
        action = (
            "verify"
            if path.endswith("/session-grant")
            else "exchange"
            if path.endswith("/token")
            else None
        )
        if action is None:
            raise AssertionError("unexpected broker fixture path")
        reply = self.call(action, body=json.loads(request.data))
        if reply["status"] != 200:
            raise HTTPError(
                request.full_url,
                reply["status"],
                "synthetic broker refusal",
                {},
                BytesIO(json.dumps(reply["result"]).encode("utf-8")),
            )
        return json_response(reply["result"])

    def client(self, *, before_request=None):
        return BrokerClient(
            broker_url="https://broker.github.test/api/github/token",
            opener=self.trace.opener(self.open, origin="caller-broker"),
            before_request=before_request,
        )

    def internal_dispatches(self):
        return self.call("request_log")["result"]

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
