"""Publication readiness waits without publishing or relaxing identity checks."""

import contextlib
import io
import sys
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


if __name__ == "__main__":
    unittest.main()
