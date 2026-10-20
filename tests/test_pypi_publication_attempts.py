"""Original publication invocation stays distinct from re-exposed API attempts."""

from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
try:
    import pypi_channel_publication as publication
finally:
    sys.path.pop(0)

FIXTURE = ROOT / "tests/fixtures/regressions/release086-job-executions.json"


def captured_publication_case():
    captured = json.loads(FIXTURE.read_text())
    run = captured["run"]
    jobs = captured["jobs"]
    original = next(j for j in jobs if j["id"] == 118574049405)
    alias = next(j for j in jobs if j["id"] == 118585795321)
    return run, jobs, original, alias


class PublisherExecutionTests(unittest.TestCase):
    def setUp(self):
        self.run, self.jobs, self.original, self.alias = captured_publication_case()

    def select(self, *, required=None, publisher=None, fresh=None):
        def pages(path, key):
            if path.startswith("actions/workflows/release.yml/runs?"):
                self.assertEqual(key, "workflow_runs")
                return [copy.deepcopy(self.run)]
            self.assertEqual(path, "actions/runs/39405656742/jobs?filter=all")
            self.assertEqual(key, "jobs")
            return copy.deepcopy(self.jobs)

        with (
            patch.object(publication.docs, "api_pages", side_effect=pages),
            patch.object(
                publication.docs,
                "api",
                return_value=copy.deepcopy(self.run if fresh is None else fresh),
            ),
        ):
            return publication.publication_run(
                self.run["head_sha"],
                "v0.8.6",
                "release.yml",
                publication.ENGINE_JOBS if required is None else required,
                publication.PUBLISH_JOB if publisher is None else publisher,
            )

    def test_reexposed_publisher_preserves_actual_execution_attempt(self):
        selected = self.select()
        self.assertEqual(selected["run_attempt"], 3)
        self.assertEqual(selected["publication_attempt"], 2)
        self.assertEqual(selected["publication_job_id"], self.original["id"])

    def test_real_reexposed_build_record_preserves_its_original_attempt(self):
        captured = json.loads(FIXTURE.read_text())
        self.run = captured["previous_run"]
        self.jobs = [j for j in captured["jobs"] if j["run_attempt"] <= 2]
        name = "Build and attest release bundle"
        selected = self.select(required={name}, publisher=name)
        self.assertEqual(selected["run_attempt"], 2)
        self.assertEqual(selected["publication_attempt"], 1)
        self.assertEqual(selected["publication_job_id"], 118570950003)

    def test_genuine_new_publisher_success_and_failure_keep_latest_execution(self):
        # A synthetic new execution, unlike the observed attempt-3 alias.
        self.alias.update(
            created_at="2026-10-20T10:30:00Z",
            started_at="2026-10-20T10:31:00Z",
            completed_at="2026-10-20T10:31:10Z",
            runner_id=1000050000,
            runner_name="GitHub Actions 1000050000",
        )
        for step in self.alias["steps"]:
            step.update(
                started_at="2026-10-20T10:31:01Z",
                completed_at="2026-10-20T10:31:05Z",
            )
        selected = self.select()
        self.assertEqual(selected["publication_attempt"], 3)
        self.assertEqual(selected["publication_job_id"], self.alias["id"])
        self.alias["conclusion"] = "failure"
        with self.assertRaisesRegex(ValueError, "prerequisite"):
            self.select()

    def test_reexposed_changed_timestamps_steps_and_runner_hold(self):
        mutations = (
            ("started_at", "2026-10-20T10:21:20Z"),
            ("completed_at", "2026-10-20T10:21:48Z"),
            ("runner_id", 1000050000),
            ("runner_name", "another runner"),
            ("runner_group_id", 3),
            ("runner_group_name", "another group"),
            ("labels", ["another-label"]),
            ("workflow_name", "another workflow"),
        )
        for field, value in mutations:
            with self.subTest(field=field):
                old = self.alias[field]
                self.alias[field] = value
                with self.assertRaises(ValueError):
                    self.select()
                self.alias[field] = old
        for field, value in (
            ("name", "another step"),
            ("started_at", "2026-10-20T10:21:23Z"),
            ("completed_at", "2026-10-20T10:21:23Z"),
            ("conclusion", "skipped"),
        ):
            with self.subTest(step=field):
                old = self.alias["steps"][0][field]
                self.alias["steps"][0][field] = value
                with self.assertRaises(ValueError):
                    self.select()
                self.alias["steps"][0][field] = old

    def test_missing_malformed_and_foreign_execution_facts_hold(self):
        for field in (
            "run_id",
            "head_sha",
            "head_branch",
            "workflow_name",
            "created_at",
            "started_at",
            "completed_at",
            "runner_id",
            "runner_name",
            "runner_group_id",
            "runner_group_name",
            "labels",
            "steps",
        ):
            with self.subTest(missing=field):
                old = self.alias.pop(field)
                with self.assertRaises(ValueError):
                    self.select()
                self.alias[field] = old
        for field, value in (
            ("run_id", 999),
            ("head_sha", "d" * 40),
            ("head_branch", "v0.8.7"),
            ("runner_id", True),
            ("runner_group_id", False),
            ("runner_name", ""),
            ("labels", []),
            ("labels", ["ubuntu-latest", "ubuntu-latest"]),
            ("created_at", "invalid"),
            ("started_at", "2026-99-20T10:21:21Z"),
            ("created_at", "2026-10-20T10:21:30Z"),
            ("steps", []),
            ("steps", None),
            ("steps", [None]),
        ):
            with self.subTest(malformed=field, value=value):
                old = self.alias[field]
                self.alias[field] = value
                with self.assertRaises(ValueError):
                    self.select()
                self.alias[field] = old
        for field in (
            "name",
            "number",
            "status",
            "conclusion",
            "started_at",
            "completed_at",
        ):
            with self.subTest(missing_step=field):
                old = self.alias["steps"][0].pop(field)
                with self.assertRaises(ValueError):
                    self.select()
                self.alias["steps"][0][field] = old

    def test_missing_duplicate_or_ambiguous_original_holds(self):
        self.jobs.remove(self.original)
        with self.assertRaises(ValueError):
            self.select()
        self.jobs.append(self.original)
        for same_id, conclusion in (
            (True, "success"),
            (False, "success"),
            (False, "failure"),
        ):
            with self.subTest(same_id=same_id, conclusion=conclusion):
                duplicate = copy.deepcopy(self.original)
                if not same_id:
                    duplicate["id"] = 900000000003
                duplicate["conclusion"] = conclusion
                self.jobs.append(duplicate)
                with self.assertRaises(ValueError):
                    self.select()
                self.jobs.pop()
        self.original.pop("steps")
        with self.assertRaises(ValueError):
            self.select()

    def test_selected_and_earlier_genuine_origins_are_ambiguous(self):
        self.alias["created_at"] = self.original["created_at"]
        with self.assertRaisesRegex(ValueError, "origin is ambiguous"):
            self.select()

    def test_latest_failed_or_changed_run_is_not_masked_by_original_success(self):
        self.alias["conclusion"] = "failure"
        with self.assertRaisesRegex(ValueError, "prerequisite"):
            self.select()
        self.alias["conclusion"] = "success"
        fresh = {**self.run, "run_attempt": 4}
        with self.assertRaisesRegex(ValueError, "changed during qualification"):
            self.select(fresh=fresh)

    def test_reexposed_success_cannot_mask_a_newer_distinct_execution(self):
        self.run["run_attempt"] = 4
        self.alias["run_attempt"] = 4
        newer = copy.deepcopy(self.original)
        newer.update(
            id=900000000004,
            run_attempt=3,
            created_at="2026-10-20T10:30:00Z",
            started_at="2026-10-20T10:31:00Z",
            completed_at="2026-10-20T10:31:10Z",
            runner_id=1000050000,
            runner_name="GitHub Actions 1000050000",
        )
        for step in newer["steps"]:
            step.update(
                started_at="2026-10-20T10:31:01Z",
                completed_at="2026-10-20T10:31:05Z",
            )
        self.jobs.append(newer)
        for conclusion in ("failure", "cancelled", None, "success"):
            with self.subTest(newer_conclusion=conclusion):
                newer["conclusion"] = conclusion
                with self.assertRaises(ValueError):
                    self.select()
