#!/usr/bin/env python3
"""Run every original Worker qualification in at most two isolated processes."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Every entry retains the original interpreter flags and module entry point.
# Body and tail wrappers have independent fixtures and no module/class hooks.
# Run the longer journeys first; failures never skip another qualification.
QUALIFICATIONS = (
    ("body-limit", ("unittest", "tests.quarantine_body_worker_qualification"), {}),
    (
        "published-tail",
        ("unittest", "tests.quarantine_published_tail_worker_qualification"),
        {},
    ),
    (
        "quarantine",
        (
            "unittest",
            "tests.quarantine_worker_qualification",
            "tests.quarantine_lifecycle_transfer_qualification",
        ),
        {},
    ),
    ("feedback", ("tests.epic238_feedback_broker_qualification",), {}),
    ("broker-control", ("tests.epic238_broker_control_qualification",), {}),
    (
        "media",
        (
            "unittest",
            "tests.test_media_cli_activation",
            "tests.test_hosted_disposition_snapshots",
        ),
        {"REVIEWSENSEI_MEDIA_BROKER_QUALIFICATION": "1"},
    ),
    ("initial-admission", ("tests.epic238_initial_tail_qualification",), {}),
    ("review-caller", ("unittest", "tests.api_work_worker_qualification"), {}),
    ("command-caller", ("unittest", "tests.action_run_worker_qualification"), {}),
)


def commands():
    return tuple(
        (
            name,
            (sys.executable, "-W", "error::ResourceWarning", "-m", *modules, "-v"),
            env,
        )
        for name, modules, env in QUALIFICATIONS
    )


def stop(process):
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
        process.wait()


def run(plan, report, *, interruption=None):
    names = [name for name, _argv, _env in plan]
    if not names or len(names) != len(set(names)):
        raise ValueError("qualification names must be nonempty and unique")
    started = time.monotonic()
    active, results = {}, {}
    stopped = []
    next_index = 0
    try:
        with tempfile.TemporaryDirectory(prefix="worker-qualification-") as directory:
            try:
                while next_index < len(plan) or active:
                    if interruption is not None and interruption[0] is not None:
                        break
                    while next_index < len(plan) and len(active) < 2:
                        if interruption is not None and interruption[0] is not None:
                            break
                        index = next_index
                        next_index += 1
                        name, argv, overrides = plan[index]
                        path = Path(directory) / f"{index}.log"
                        stream = path.open("wb")
                        begin = time.monotonic()
                        print(f"Starting Worker qualification: {name}", flush=True)
                        try:
                            process = subprocess.Popen(
                                argv,
                                env={**os.environ, **overrides},
                                stdout=stream,
                                stderr=subprocess.STDOUT,
                                start_new_session=os.name == "posix",
                            )
                        except OSError as error:
                            stream.close()
                            results[index] = {
                                "name": name,
                                "returncode": None,
                                "launch_error": type(error).__name__,
                                "seconds": 0,
                            }
                        else:
                            active[index] = (process, stream, path, begin)
                    for index, (process, stream, path, begin) in tuple(active.items()):
                        code = process.poll()
                        if code is None:
                            continue
                        stream.close()
                        name = plan[index][0]
                        results[index] = {
                            "name": name,
                            "returncode": code,
                            "seconds": time.monotonic() - begin,
                        }
                        print(f"Worker qualification {name}: exit {code}", flush=True)
                        with path.open(encoding="utf-8", errors="replace") as log:
                            for line in log:
                                print(line, end="")
                        del active[index]
                    if active:
                        time.sleep(0.05)
            finally:
                for index, (process, stream, _path, _begin) in active.items():
                    try:
                        stop(process)
                        stopped.append(
                            {"name": plan[index][0], "returncode": process.returncode}
                        )
                    finally:
                        stream.close()
    finally:
        interrupt_signal = None if interruption is None else interruption[0]
        complete = (
            interrupt_signal is None
            and len(results) == len(plan)
            and all(row["returncode"] is not None for row in results.values())
        )
        document = {
            "schema_version": "worker-qualification-processes-v1",
            "maximum_parallel_processes": 2,
            "source_sha": os.environ.get("GITHUB_SHA"),
            "seconds": time.monotonic() - started,
            "plan": [
                {"name": name, "argv": list(argv), "overrides": env}
                for name, argv, env in plan
            ],
            "results": [results[index] for index in sorted(results)],
            "stopped": stopped,
            "interrupted_signal": interrupt_signal,
            "complete": complete,
            "success": complete
            and all(row["returncode"] == 0 for row in results.values()),
        }
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return 0 if document["success"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    interruption = [None]

    def request_stop(signum, _frame):
        # Do not raise from the handler: interruption between Popen and storing
        # its handle could otherwise leave a child outside the cleanup registry.
        if interruption[0] is None:
            interruption[0] = signum

    previous = {}
    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, request_stop)
        code = run(commands(), args.report, interruption=interruption)
        return code if interruption[0] is None else 128 + interruption[0]
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())
