"""Synthetic processes only: no production Worker journeys execute here."""

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
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
