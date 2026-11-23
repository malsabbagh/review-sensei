"""Exercise pre-move admission with authenticated API/ZIP fixtures, without writes to GitHub."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
try:
    import qualify_v5_candidate as candidate
finally:
    sys.path.pop(0)

SOURCE, LEASE, CORE, HOSTED = (s * 40 for s in "abcd")


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.run = {
            "id": 30,
            "run_attempt": 1,
            "path": ".github/workflows/published-cli.yml",
            "event": "workflow_run",
            "head_branch": "main",
            "head_sha": SOURCE,
            "repository": {"full_name": candidate.docs.REPOSITORY},
            "head_repository": {"full_name": candidate.docs.REPOSITORY},
            "status": "completed",
            "conclusion": "success",
            "display_title": "Qualify published CLI after 20/1",
            "created_at": "2026-01-01T00:00:00Z",
        }
        self.core = {
            "source": SOURCE,
            "tag_object": CORE,
            "tag": "v1.2.3",
            "version": "1.2.3",
            "run_id": 10,
            "run_attempt": 1,
            "workflow": "release.yml",
        }
        self.hosted = {
            "source": SOURCE,
            "tag_object": HOSTED,
            "tag": "github-v0.2.1",
            "version": "0.2.1",
            "run_id": 20,
            "run_attempt": 1,
            "workflow": "release-github.yml",
        }
        self.receipt = {
            "status": "PASS",
            "wheel_sha256": "e" * 64,
            "github_wheel_sha256": "f" * 64,
            "pypi_publication": {
                "projects": {
                    "review-sensei": self.core,
                    "review-sensei-github": self.hosted,
                }
            },
        }
        self.files = {
            "published-cli-publication-gate": {
                "publication-gate.json": {
                    "status": "PASS",
                    "mode": "confirmed-hosted-pypi-publish",
                    "signed_sources_rechecked": True,
                    "core_tag_object": CORE,
                    "core_source_sha": SOURCE,
                    "github_tag_object": HOSTED,
                    "source_sha": SOURCE,
                    "version": "1.2.3",
                    "github_version": "0.2.1",
                    "release_run_id": 20,
                    "release_attempt": 1,
                },
                "core-publication-readiness.json": {**self.core, "status": "PASS"},
            }
        }
        self.jobs = ["Require exact successful PyPI publication"]
        for os_name in ("ubuntu-latest", "macos-latest"):
            for py in ("3.11", "3.14"):
                name = f"published-cli-{os_name}-py{py}"
                self.jobs.append(f"PyPI 1.2.3 / {os_name} / Python {py}")
                self.files[name] = {
                    "summary.json": {
                        "status": "PASS",
                        "mode": "current-lifecycle",
                        "selected_suites": list(candidate.SUITES),
                        "excluded_suites": [],
                        "requested_version": "1.2.3",
                        "requested_github_version": "0.2.1",
                        "wheel": {"sha256": "e" * 64},
                        "github_wheel": {"sha256": "f" * 64},
                    }
                }
        self.jobs = [
            {
                "id": index,
                "run_id": 30,
                "head_sha": SOURCE,
                "started_at": "2026-01-01T00:00:10Z",
                "completed_at": "2026-01-01T00:01:00Z",
                "run_attempt": 1,
                "name": name,
                "status": "completed",
                "conclusion": "success",
            }
            for index, name in enumerate(self.jobs, 1)
        ]
        self.blobs = {}
        self.runs = [self.run]
        self.artifact_changes = {}
        self.ci_run = {
            "id": 40,
            "run_attempt": 1,
            "head_sha": SOURCE,
            "head_branch": "main",
            "event": "push",
            "path": ".github/workflows/ci.yml",
            "repository": self.run["repository"],
            "head_repository": self.run["head_repository"],
            "status": "completed",
            "conclusion": "success",
        }
        self.ci_jobs = [
            {
                "name": name,
                "run_attempt": 1,
                "status": "completed",
                "conclusion": "success",
            }
            for name in candidate.prepare.CI_JOBS
        ]

        self.enterContext(patch.object(candidate.docs, "api", side_effect=self.api))
        self.enterContext(
            patch.object(candidate.docs, "api_pages", side_effect=self.pages)
        )
        self.enterContext(
            patch.object(candidate.docs, "gh_run", side_effect=self.download)
        )
        self.enterContext(
            patch.object(candidate.gate, "qualify", return_value=self.receipt)
        )
        self.current = self.enterContext(patch.object(candidate.gate, "assert_current"))
        self.publication = self.enterContext(
            patch.object(
                candidate.gate.pypi,
                "publication_run",
                side_effect=lambda source, tag, *args: {
                    "run_attempt": 1,
                    "run_id": 10 if tag == "v1.2.3" else 20,
                },
            )
        )

    def api(self, path):
        if path == "actions/runs/30":
            return copy.deepcopy(self.run)
        if path == "actions/runs/40":
            return self.ci_run
        if path.startswith("compare/"):
            return {"status": "identical"}
        self.fail(path)

    def pages(self, path, key):
        if path.startswith("actions/workflows/ci.yml/runs?"):
            return [self.ci_run]
        if path == "actions/runs/40/jobs?filter=all":
            return self.ci_jobs
        if path.startswith("actions/workflows/published-cli.yml/runs?"):
            return self.runs
        if path == "actions/runs/30/jobs?filter=all":
            return self.jobs
        self.assertEqual(path, "actions/runs/30/artifacts")
        result = []
        for index, (name, files) in enumerate(self.files.items(), 1):
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as archive:
                for member, data in files.items():
                    archive.writestr(member, json.dumps(data))
            raw = buf.getvalue()
            self.blobs[index] = raw
            result.append(
                {
                    "id": index,
                    "name": name,
                    "workflow_run": {"id": 30, "head_sha": SOURCE},
                    "expired": False,
                    "created_at": "2026-01-01T00:00:45Z",
                    "size_in_bytes": len(raw),
                    "digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
                    **self.artifact_changes,
                }
            )
        return result

    def download(self, command, stage):
        self.assertEqual(command[:2], ["gh", "api"])
        return SimpleNamespace(stdout=self.blobs[int(command[2].split("/")[-2])])

    def qualify(self):
        return candidate.qualify(SOURCE, LEASE, 30, self.root)

    def test_absent_failed_and_pending_npm_admit_the_same_exact_python_candidate(self):
        for state in ("absent", "failure", "pending"):
            with self.subTest(state=state):
                if state != "absent":
                    active = state == "pending"
                    self.ci_run.update(
                        status="in_progress" if active else "completed",
                        conclusion=None if active else "failure",
                    )
                    self.ci_jobs.extend(
                        {
                            "name": name,
                            "status": "queued" if active else "completed",
                            "conclusion": None if active else "failure",
                            "run_attempt": 1,
                        }
                        for name in candidate.prepare.NPM_CI_JOBS | {"Required checks"}
                    )
                receipt = self.qualify()
                self.assertEqual(len(receipt["installed_python"]["artifacts"]), 5)
                self.assertEqual(
                    len(
                        {
                            a["job_id"]
                            for a in receipt["installed_python"]["artifacts"].values()
                        }
                    ),
                    5,
                )
                self.assertTrue((self.root / "v5-candidate.json").is_file())
        self.assertEqual(self.current.call_count, 3)
        self.current.assert_called_with(self.receipt, before_move=True)

    def test_current_publication_and_all_installed_lanes_required(self):
        lane = self.jobs[-1]
        for changes in [
            {"status": "queued", "conclusion": None},
            {"conclusion": "failure"},
            {"conclusion": "skipped"},
            {"id": None},
        ]:
            with (
                self.subTest(changes=changes),
                patch.dict(lane, changes),
                self.assertRaises(ValueError),
            ):
                self.qualify()
        self.jobs.pop()
        with self.assertRaisesRegex(ValueError, "job did not succeed"):
            self.qualify()
        self.assertFalse((self.root / "v5-candidate.json").exists())

    def test_wrong_bytes_missing_receipt_old_run_manual_or_pending_refused(self):
        lane = "published-cli-ubuntu-latest-py3.11"
        for changes in [
            {"wheel": {"sha256": "9" * 64}},
            {"github_wheel": {"sha256": "0" * 64}},
            {"status": "FAIL"},
            {"requested_github_version": "0.1.0"},
            {"mode": "historical-regression"},
            {"selected_suites": []},
        ]:
            with (
                self.subTest(changes=changes),
                patch.dict(self.files[lane]["summary.json"], changes),
                self.assertRaisesRegex(ValueError, "qualified wheels"),
            ):
                self.qualify()
        for changes in [
            {"expired": True},
            {"created_at": "2025-01-01T00:00:00Z"},
            {"digest": "sha256:" + "0" * 64},
            {"workflow_run": {"id": 9, "head_sha": SOURCE}},
        ]:
            with (
                patch.dict(self.artifact_changes, changes),
                self.assertRaises(ValueError),
            ):
                self.qualify()
        for changes in [
            {"event": "workflow_dispatch"},
            {"conclusion": "failure"},
            {"status": "in_progress", "conclusion": None},
            {"display_title": "Qualify published CLI after 20/2"},
            {"head_branch": "feature"},
        ]:
            with patch.dict(self.run, changes), self.assertRaises(ValueError):
                self.qualify()
        self.files.pop(lane)
        with self.assertRaisesRegex(ValueError, "missing or ambiguous"):
            self.qualify()

    def test_stale_publication_or_engineering_ci_refuses_before_move(self):
        for name, key in [
            ("publication-gate.json", "core_tag_object"),
            ("publication-gate.json", "release_attempt"),
            ("core-publication-readiness.json", "source"),
        ]:
            with (
                patch.dict(
                    self.files["published-cli-publication-gate"][name], {key: "stale"}
                ),
                self.assertRaisesRegex(ValueError, "current signed"),
            ):
                self.qualify()
        with (
            patch.object(
                candidate.gate.pypi, "publication_run", return_value={"run_attempt": 2}
            ),
            self.assertRaisesRegex(ValueError, "attempt changed"),
        ):
            self.qualify()
        with (
            patch.dict(self.ci_jobs[0], {"conclusion": "failure"}),
            self.assertRaises(ValueError),
        ):
            self.qualify()
        self.ci_jobs.pop()
        with self.assertRaisesRegex(ValueError, "missing"):
            self.qualify()
        self.assertFalse((self.root / "v5-candidate.json").exists())

    def test_installed_retry_and_changed_lease_cannot_reuse_old_evidence(self):
        self.runs.append({**self.run, "id": 31})
        with self.assertRaisesRegex(ValueError, "stale or ambiguous"):
            self.qualify()
        self.runs.pop()
        self.current.side_effect = ValueError("major tag changed")
        with self.assertRaisesRegex(ValueError, "major tag changed"):
            self.qualify()
        self.assertFalse((self.root / "v5-candidate.json").exists())
