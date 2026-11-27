"""Synthetic processes only: no production Worker journeys execute here."""

import contextlib
import importlib.util
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[1] / "scripts/run_worker_qualifications.py"
SPEC = importlib.util.spec_from_file_location("worker_qualification_runner", PATH)
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)


class WorkerQualificationRunnerTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "POSIX process-group signal contract")
    def test_sigterm_stops_owned_children_even_when_they_exit_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child = (
                "import os,signal,sys,time; from pathlib import Path; "
                "signal.signal(signal.SIGTERM,lambda *_:sys.exit(0)); "
                "Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)"
            )
            script = (
                "import importlib.util,sys; from pathlib import Path; "
                f"spec=importlib.util.spec_from_file_location('runner',{str(PATH)!r}); "
                "runner=importlib.util.module_from_spec(spec); spec.loader.exec_module(runner); "
                "root=Path(sys.argv[1]); "
                f"child={child!r}; "
                "runner.commands=lambda:tuple((str(i),(sys.executable,'-c',child,str(root/f'{i}.pid')),{}) for i in range(3)); "
                "sys.argv=['runner','--report',str(root/'report.json')]; "
                "sys.exit(runner.main())"
            )
            with (root / "controller.log").open("w") as output:
                process = subprocess.Popen(
                    (sys.executable, "-c", script, str(root)),
                    stdout=output,
                    stderr=subprocess.STDOUT,
                )
                ceased = set()
                try:
                    deadline = time.monotonic() + 10
                    while not all((root / f"{i}.pid").exists() for i in range(2)):
                        self.assertIsNone(
                            process.poll(), "controller exited before children"
                        )
                        self.assertLess(
                            time.monotonic(), deadline, "children did not start"
                        )
                        time.sleep(0.02)
                    process.send_signal(signal.SIGTERM)
                    self.assertEqual(process.wait(timeout=15), 128 + signal.SIGTERM)
                    document = json.loads((root / "report.json").read_text())
                    self.assertFalse(document["complete"])
                    self.assertFalse(document["success"])
                    self.assertEqual(document["interrupted_signal"], signal.SIGTERM)
                    self.assertEqual(
                        document["stopped"],
                        [{"name": str(i), "returncode": 0} for i in range(2)],
                    )
                    self.assertFalse((root / "2.pid").exists())
                    for i in range(2):
                        pid = int((root / f"{i}.pid").read_text())
                        with self.assertRaises(ProcessLookupError):
                            os.kill(pid, 0)
                        ceased.add(pid)
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.wait()
                    for path in root.glob("*.pid"):
                        pid = int(path.read_text())
                        if pid in ceased:
                            continue
                        try:
                            os.killpg(pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass

    def test_interrupted_controller_reaps_children_and_reports_incomplete(self):
        with tempfile.TemporaryDirectory() as directory:
            processes = []
            original = RUNNER.subprocess.Popen

            def start(*args, **kwargs):
                process = original(*args, **kwargs)
                processes.append(process)
                return process

            report = Path(directory) / "report.json"
            plan = tuple(
                (str(i), (sys.executable, "-c", "import time; time.sleep(60)"), {})
                for i in range(3)
            )
            with (
                contextlib.redirect_stdout(io.StringIO()),
                patch.object(RUNNER.subprocess, "Popen", side_effect=start),
                patch.object(
                    RUNNER,
                    "time",
                    SimpleNamespace(
                        monotonic=RUNNER.time.monotonic,
                        sleep=lambda _: (_ for _ in ()).throw(KeyboardInterrupt()),
                    ),
                ),
                self.assertRaises(KeyboardInterrupt),
            ):
                RUNNER.run(plan, report)
            self.assertEqual(len(processes), 2)
            self.assertTrue(all(process.poll() is not None for process in processes))
            self.assertFalse(json.loads(report.read_text())["complete"])

    def test_plan_retains_every_original_entry_point_and_special_environment(self):
        expected = (
            "tests.epic238_feedback_broker_qualification",
            "tests.epic238_broker_control_qualification",
            "tests.test_media_cli_activation",
            "tests.test_hosted_disposition_snapshots",
            "tests.epic238_initial_tail_qualification",
            "tests.api_work_worker_qualification",
            "tests.action_run_worker_qualification",
            "tests.quarantine_worker_qualification",
            "tests.quarantine_lifecycle_transfer_qualification",
            "tests.quarantine_body_worker_qualification",
            "tests.quarantine_published_tail_worker_qualification",
            "tests.v6_one_writer_worker_qualification",
        )
        plan = RUNNER.commands()
        actual = []
        for name, argv, env in plan:
            self.assertEqual(
                argv[:4], (sys.executable, "-W", "error::ResourceWarning", "-m")
            )
            self.assertEqual(argv[-1], "-v")
            modules = argv[4:-1]
            actual.extend(module for module in modules if module != "unittest")
            if name in {"feedback", "broker-control", "initial-admission"}:
                self.assertNotEqual(modules[0], "unittest")
            else:
                self.assertEqual(modules[0], "unittest")
            self.assertEqual(
                env,
                {"REVIEWSENSEI_MEDIA_BROKER_QUALIFICATION": "1"}
                if name == "media"
                else {},
            )
        self.assertEqual(Counter(actual), Counter(expected))

    def test_each_process_runs_once_and_failure_does_not_skip_remaining_work(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = (
                "import json,os,sys,time; from pathlib import Path; "
                "start=time.monotonic(); time.sleep(.15); "
                "Path(sys.argv[1]).write_text(json.dumps([start,time.monotonic(),os.getenv('QUALIFICATION_SENTINEL')])); "
                "sys.exit(int(sys.argv[2]))"
            )
            plan = tuple(
                (
                    str(i),
                    (sys.executable, "-c", script, str(root / f"{i}.json"), str(code)),
                    {"QUALIFICATION_SENTINEL": str(i)},
                )
                for i, code in enumerate((0, 3, 0, 0))
            )
            with contextlib.redirect_stdout(io.StringIO()):
                code = RUNNER.run(plan, root / "report.json")
            self.assertEqual(code, 1)
            report = json.loads((root / "report.json").read_text())
            self.assertTrue(report["complete"])
            self.assertFalse(report["success"])
            self.assertEqual(
                [row["returncode"] for row in report["results"]], [0, 3, 0, 0]
            )
            events = []
            for i in range(4):
                start, end, sentinel = json.loads((root / f"{i}.json").read_text())
                self.assertEqual(sentinel, str(i))
                events.extend(((start, 1), (end, -1)))
            active = peak = 0
            for _stamp, delta in sorted(events):
                active += delta
                peak = max(peak, active)
            self.assertEqual(active, 0)
            self.assertEqual(peak, 2)

    def test_launch_failure_is_incomplete_and_does_not_skip_next_process(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.json"
            plan = (
                ("missing", (str(Path(directory) / "missing"),), {}),
                ("next", (sys.executable, "-c", "pass"), {}),
            )
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(RUNNER.run(plan, report), 1)
            document = json.loads(report.read_text())
            self.assertFalse(document["complete"])
            self.assertFalse(document["success"])
            self.assertEqual(
                [row["name"] for row in document["results"]], ["missing", "next"]
            )
            self.assertEqual(document["results"][1]["returncode"], 0)


if __name__ == "__main__":
    unittest.main()
