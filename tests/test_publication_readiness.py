"""Publication readiness waits without publishing or relaxing identity checks."""

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import wait_for_publication as readiness  # noqa: E402

SOURCE = "a" * 40


def run(**changes):
    return {
        "id": 42,
        "run_attempt": 1,
        "path": ".github/workflows/release.yml",
        "event": "push",
        "head_branch": "v1.2.3",
        "head_sha": SOURCE,
        "repository": {"full_name": readiness.REPOSITORY},
        "head_repository": {"full_name": readiness.REPOSITORY},
        "status": "completed",
        "conclusion": "success",
        **changes,
    }


def job(attempt=1, **changes):
    return {
        "id": 99,
        "name": readiness.WORKFLOWS["release.yml"][1],
        "run_attempt": attempt,
        "status": "completed",
        "conclusion": "success",
        **changes,
    }


class PublicationReadinessTests(unittest.TestCase):
    def wait(self, snapshots, jobs=None, **kwargs):
        report = {"status": "ERROR"}
        reader = Mock(side_effect=[*snapshots, snapshots[-1]])
        jobs_reader = Mock(return_value=[job()] if jobs is None else jobs)
        pauses = []
        with contextlib.redirect_stdout(io.StringIO()):
            readiness.wait_for_publication(
                "release.yml",
                "v1.2.3",
                SOURCE,
                report,
                read=reader,
                read_jobs=jobs_reader,
                sleep=pauses.append,
                clock=Mock(return_value=0),
                **kwargs,
            )
        return report, reader, jobs_reader, pauses

    def test_pending_publication_and_owner_recovery_wait_before_verification(self):
        report, reads, jobs, pauses = self.wait(
            [
                None,
                run(status="waiting", conclusion=None),
                run(conclusion="failure"),
                run(run_attempt=2, status="in_progress", conclusion=None),
                run(run_attempt=2),
            ],
            jobs=[job(), job(2)],
        )
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["run_attempt"], 2)
        self.assertEqual(reads.call_count, 6)
        self.assertEqual(pauses, [30] * 4)
        jobs.assert_called_once_with("actions/runs/42/jobs?filter=all", "jobs")

    def test_publisher_retained_from_original_attempt_is_valid_after_finalizer_retry(
        self,
    ):
        report, _, _, pauses = self.wait([run(run_attempt=2)])
        self.assertEqual(report["publication_job_id"], 99)
        self.assertFalse(pauses)

    def test_hosted_publication_uses_its_own_workflow_tag_and_publisher(self):
        report = {"status": "ERROR"}
        reader = Mock(return_value=run())
        hosted_job = job(name=readiness.WORKFLOWS["release-github.yml"][1])
        readiness.wait_for_publication(
            "release-github.yml",
            "github-v0.3.11",
            SOURCE,
            report,
            read=reader,
            read_jobs=Mock(return_value=[hosted_job]),
        )
        self.assertEqual(report["status"], "PASS")
        reader.assert_called_with("release-github.yml", "github-v0.3.11", SOURCE)

    def test_npm_waits_for_terminal_consumers_and_recorder_before_verification(self):
        report = {"status": "ERROR"}
        pending = run(
            path=".github/workflows/publish-npm.yml",
            status="in_progress",
            conclusion=None,
        )
        completed = {**pending, "status": "completed", "conclusion": "success"}
        reader = Mock(side_effect=[pending, completed, completed])
        jobs = Mock(
            return_value=[job(name="Publish npm packages with Trusted Publishing")]
        )
        pauses = []
        with contextlib.redirect_stdout(io.StringIO()):
            readiness.wait_for_publication(
                "publish-npm.yml",
                "v1.2.3",
                SOURCE,
                report,
                read=reader,
                read_jobs=jobs,
                sleep=pauses.append,
                clock=Mock(return_value=0),
            )
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(pauses, [30])
        self.assertEqual(reader.call_count, 3)
        jobs.assert_called_once_with("actions/runs/42/jobs?filter=all", "jobs")

    def test_failed_npm_consumer_run_does_not_admit_documentation(self):
        report = {"status": "ERROR"}
        jobs = Mock(
            return_value=[job(name="Publish npm packages with Trusted Publishing")]
        )
        with (
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaises(readiness.PublicationTimeout),
        ):
            readiness.wait_for_publication(
                "publish-npm.yml",
                "v1.2.3",
                SOURCE,
                report,
                read=Mock(
                    return_value=run(
                        path=".github/workflows/publish-npm.yml", conclusion="failure"
                    )
                ),
                read_jobs=jobs,
                sleep=Mock(),
                clock=Mock(side_effect=[0, 10]),
                timeout=10,
            )
        self.assertEqual(report["status"], "ERROR")
        jobs.assert_not_called()

    def test_new_attempt_during_job_readback_does_not_admit_verification(self):
        with self.assertRaisesRegex(ValueError, "changed during"):
            self.wait(
                [run(), run(run_attempt=2, status="in_progress", conclusion=None)]
            )

    def test_completed_run_does_not_waive_failed_missing_or_duplicate_publisher(self):
        for jobs in ([], [job(conclusion="failure")], [job(), job()], [job(2)]):
            with self.subTest(jobs=jobs), self.assertRaises(ValueError):
                self.wait([run()], jobs=jobs)

    def test_changed_original_run_and_regressed_attempt_fail_closed(self):
        for changed in (run(id=43), run(run_attempt=1)):
            with (
                self.subTest(changed=changed),
                self.assertRaisesRegex(ValueError, "regressed"),
            ):
                self.wait([run(run_attempt=2, status="in_progress"), changed])

    def test_timeout_is_a_failure_without_a_success_receipt(self):
        report = {"status": "ERROR"}
        sleep = Mock()
        with self.assertRaisesRegex(ValueError, "timed out"):
            readiness.wait_for_publication(
                "release.yml",
                "v1.2.3",
                SOURCE,
                report,
                read=Mock(return_value=None),
                sleep=sleep,
                clock=Mock(side_effect=[0, 10]),
                timeout=10,
            )
        self.assertEqual(report["status"], "ERROR")
        sleep.assert_not_called()

    def test_exact_source_repository_workflow_and_unique_run_remain_required(self):
        for field, value in (
            ("head_sha", "b" * 40),
            ("head_branch", "v9.9.9"),
            ("event", "workflow_dispatch"),
            ("path", ".github/workflows/other.yml"),
            ("head_repository", {"full_name": "fork/repo"}),
            ("repository", {"full_name": "fork/repo"}),
            ("id", True),
        ):
            with (
                self.subTest(field=field),
                patch.object(readiness, "pages", return_value=[run(**{field: value})]),
            ):
                with self.assertRaises(ValueError):
                    readiness.publication_run("release.yml", "v1.2.3", SOURCE)
        with patch.object(readiness, "pages", return_value=[run(), run()]):
            with self.assertRaisesRegex(ValueError, "one original"):
                readiness.publication_run("release.yml", "v1.2.3", SOURCE)

    def test_access_denied_is_not_treated_as_publication_pending(self):
        with self.assertRaisesRegex(ValueError, "access failed"):
            self.wait([HTTPError("https://api.github.com", 403, "private", None, None)])

    def test_transient_api_failure_can_recover_without_repeating_publication(self):
        report, _, _, pauses = self.wait(
            [HTTPError("https://api.github.com", 503, "private", None, None), run()]
        )
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(pauses, [30])


class PagesPublicationReadinessTests(unittest.TestCase):
    def setUp(self):
        self.report = {"status": "ERROR", "workflow": "publish-npm.yml"}
        self.core = run()
        self.read = Mock(side_effect=lambda path: self.core)
        self.original = Mock(side_effect=lambda *args: self.core)
        self.jobs = [
            job(name=name)
            for name in (
                "Build release documentation",
                "Publish to PyPI with Trusted Publishing",
                "Create GitHub release assets",
            )
        ]
        self.read_jobs = Mock(side_effect=lambda *args: self.jobs)
        self.wait = Mock(side_effect=lambda *args: args[-1].update(status="PASS"))

    def invoke(self):
        readiness.wait_for_pages_publication(
            42,
            "v1.2.3",
            SOURCE,
            self.report,
            read=self.read,
            read_jobs=self.read_jobs,
            read_original=self.original,
            wait=self.wait,
        )

    def test_split_waits_for_the_exact_original_npm_run(self):
        self.invoke()
        self.assertEqual(self.report["status"], "PASS")
        self.assertEqual(self.report["mode"], "split")
        self.original.assert_called_once_with("release.yml", "v1.2.3", SOURCE)
        self.wait.assert_called_once_with(
            "publish-npm.yml", "v1.2.3", SOURCE, self.report
        )
        self.read_jobs.assert_called_once_with(
            "actions/runs/42/jobs?filter=all", "jobs"
        )

    def test_combined_recovery_does_not_wait_for_a_nonexistent_independent_run(self):
        self.core["run_attempt"] = 2
        # A publisher retained from attempt one remains valid under legacy policy.
        self.jobs.append(job(name="Publish npm packages with Trusted Publishing"))
        self.invoke()
        self.assertEqual(self.report["status"], "PASS")
        self.assertEqual(self.report["mode"], "combined")
        self.assertEqual(self.report["workflow"], "release.yml")
        self.original.assert_not_called()
        self.wait.assert_not_called()

    def test_combined_failed_or_ambiguous_publisher_never_skips_its_gate(self):
        self.core["run_attempt"] = 2
        original = list(self.jobs)
        publisher = job(name="Publish npm packages with Trusted Publishing")
        for additions in (
            [{**publisher, "conclusion": "failure"}],
            [publisher, {**publisher, "run_attempt": 2, "conclusion": "skipped"}],
            [publisher, dict(publisher)],
        ):
            with self.subTest(additions=additions), self.assertRaises(ValueError):
                self.jobs = original + additions
                self.invoke()
            self.assertEqual(self.report["status"], "ERROR")
            self.wait.assert_not_called()

    def test_foreign_or_incomplete_core_never_starts_waiting(self):
        original = dict(self.core)
        for field, value in (
            ("id", 43),
            ("head_branch", "v9.9.9"),
            ("head_sha", "b" * 40),
            ("path", ".github/workflows/other.yml"),
            ("event", "workflow_dispatch"),
            ("head_repository", {"full_name": "fork/repo"}),
            ("status", "in_progress"),
            ("run_attempt", True),
        ):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.core = {**original, field: value}
                self.invoke()
            self.wait.assert_not_called()
            self.assertEqual(self.report["status"], "ERROR")

    def test_partial_inventory_or_changed_original_run_refuses(self):
        self.read_jobs.side_effect = ValueError("bounded pagination incomplete")
        with self.assertRaises(ValueError):
            self.invoke()
        self.read_jobs.side_effect = lambda *args: self.jobs
        self.original.side_effect = lambda *args: {**self.core, "run_attempt": 2}
        with self.assertRaisesRegex(ValueError, "original core"):
            self.invoke()
        self.wait.assert_not_called()

    def test_readiness_timeout_never_becomes_a_success_receipt(self):
        self.wait.side_effect = readiness.PublicationTimeout("bounded timeout")
        with self.assertRaises(readiness.PublicationTimeout):
            self.invoke()
        self.assertEqual(self.report["status"], "ERROR")

    def test_pages_classification_cannot_skip_python_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.json"
            with (
                patch.object(
                    sys,
                    "argv",
                    [
                        "wait_for_publication.py",
                        "--workflow",
                        "release.yml",
                        "--tag",
                        "v1.2.3",
                        "--source",
                        SOURCE,
                        "--pages-core-run-id",
                        "42",
                        "--report",
                        str(report),
                    ],
                ),
                patch.object(readiness, "wait_for_pages_publication") as wait_pages,
                patch.object(readiness, "wait_for_publication") as wait_python,
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(readiness.main(), 1)
            wait_pages.assert_not_called()
            wait_python.assert_not_called()
            self.assertIn('"status": "ERROR"', report.read_text())


if __name__ == "__main__":
    unittest.main()
