"""Durable synthetic host and real process barriers, never production grants."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def save(path: Path, value: object) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    if os.name == "nt":
        # Windows does not expose a directory descriptor for fsync here.
        return
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def barrier(root: Path, requested: str, boundary: str) -> None:
    if boundary == requested:
        save(root / "signal.json", {"boundary": boundary})
        sys.stdin.readline()
        raise AssertionError("a killed checkpoint must never continue")


def kill_at(module: str, root: Path, boundary: str) -> None:
    """Kill the child from the parent after its fsynced boundary is visible."""
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(
            ("REVIEWSENSEI_", "OLLAMA_", "OPENAI_", "ANTHROPIC_", "GITHUB_", "GH_")
        )
        and not key.endswith(("_TOKEN", "_API_KEY", "_SECRET"))
    }
    with subprocess.Popen(
        [sys.executable, "-m", module, str(root), boundary],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
    ) as child:
        deadline = time.monotonic() + 20
        while not (root / "signal.json").exists():
            if child.poll() is not None:
                output, error = child.communicate()
                raise AssertionError(
                    (child.returncode, output.decode(), error.decode())
                )
            if time.monotonic() >= deadline:
                child.kill()
                output, error = child.communicate()
                raise AssertionError(
                    ("checkpoint timeout", output.decode(), error.decode())
                )
            time.sleep(0.01)
        assert load(root / "signal.json")["boundary"] == boundary
        child.kill()
        child.communicate(timeout=5)
        assert child.returncode is not None and child.returncode != 0
        if os.name != "nt":
            assert child.returncode < 0
