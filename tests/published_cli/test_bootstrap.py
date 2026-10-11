"""Credential-free publication/installer contracts; no network or package install."""

import argparse
import base64
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from published_cli_release_gate import (
    main as gate_main,
)
from published_cli_release_gate import (
    published_job,
    release_identity,
)
from qualify_published_cli import (
    clean_environment,
    download_command,
    exact_version,
    propagation_download,
    validate_receipt,
)


class BootstrapContracts(unittest.TestCase):
    def test_exact_version_never_accepts_latest_or_ranges_or_shell_text(self):
        for value in [
            "latest",
            "0.6",
            "0.6.20 ",
            "v0.6.20",
            ">=0.6.20",
            "0.6.20; echo bad",
            "0.6.20\n",
        ]:
            with (
                self.subTest(value=value),
                self.assertRaises(argparse.ArgumentTypeError),
            ):
                exact_version(value)
        self.assertEqual(exact_version("0.6.20"), "0.6.20")
        self.assertEqual(exact_version("0.7.0rc1"), "0.7.0rc1")

    def test_clean_environment_drops_credentials_and_pip_overrides(self):
        with patch.dict(
            os.environ,
            {
                "OLLAMA_API_KEY": "synthetic",
                "GITHUB_TOKEN": "synthetic",
                "PIP_INDEX_URL": "https://wrong.invalid",
                "PYTHONPATH": "/source",
            },
            clear=True,
        ):
            env = clean_environment(Path("/isolated"))
        for name in ["OLLAMA_API_KEY", "GITHUB_TOKEN", "PIP_INDEX_URL", "PYTHONPATH"]:
            self.assertNotIn(name, env)
        self.assertEqual(env["HOME"], "/isolated")

    def test_download_binds_exact_public_wheel_and_ignores_pip_config(self):
        command = download_command("python", "0.6.20", Path("wheels"))
        self.assertEqual(command[-1], "review-sensei==0.6.20")
        for value in [
            "--isolated",
            "--no-deps",
            "--only-binary=:all:",
            "https://pypi.org/simple",
        ]:
            self.assertIn(value, command)

    def test_propagation_stops_on_first_success_without_changing_command(self):
        calls, pauses = [], []

        def run(command, **kwargs):
            calls.append((command[:], kwargs["timeout"]))
            return subprocess.CompletedProcess(command, 0 if len(calls) == 3 else 1)

        ok, receipts = propagation_download(
            ["pip", "review-sensei==0.6.20"],
            Path("."),
            {},
            run=run,
            sleep=pauses.append,
        )
        self.assertTrue(ok)
        self.assertEqual(len(receipts), 3)
        self.assertEqual(pauses, [15, 15])
        self.assertTrue(all(c == (["pip", "review-sensei==0.6.20"], 45) for c in calls))

    def test_propagation_timeout_is_bounded_and_non_success(self):
        calls, pauses = [], []

        def run(command, **kwargs):
            calls.append(command)
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])

        ok, receipts = propagation_download(
            ["pip"], Path("."), {}, run=run, sleep=pauses.append
        )
        self.assertFalse(ok)
        self.assertEqual(len(calls), 6)
        self.assertEqual(len(pauses), 5)
        self.assertTrue(all(r["exit"] == "TIMEOUT" for r in receipts))

    def test_gate_requires_exact_successful_publication_job(self):
        self.assertEqual(
            published_job(
                [
                    {
                        "id": 1,
                        "name": "Publish to PyPI with Trusted Publishing",
                        "conclusion": "success",
                    }
                ]
            ),
            1,
        )
        for jobs in [
            [],
            [
                {
                    "name": "Publish to PyPI with Trusted Publishing",
                    "conclusion": "failure",
                }
            ],
            [
                {
                    "name": "Publish to PyPI with Trusted Publishing",
                    "conclusion": "skipped",
                }
            ],
            [
                {
                    "name": "Publish npm packages with Trusted Publishing",
                    "conclusion": "success",
                }
            ],
        ]:
            with self.subTest(jobs=jobs), self.assertRaises(ValueError):
                published_job(jobs)

    def test_gate_rejects_forks_wrong_workflows_and_nonpush_events(self):
        original = {
            "repository": {"full_name": "owner/repo"},
            "head_repository": {"full_name": "owner/repo"},
            "event": "push",
            "path": ".github/workflows/release.yml",
            "status": "completed",
            "head_sha": "a" * 40,
            "id": 1,
            "run_attempt": 1,
        }
        self.assertEqual(
            release_identity({"workflow_run": original}, "owner/repo"), original
        )
        for key, value in [
            ("head_repository", {"full_name": "fork/repo"}),
            ("event", "pull_request"),
            ("path", ".github/workflows/other.yml"),
            ("status", "in_progress"),
            ("head_sha", "main"),
            ("id", True),
            ("run_attempt", 0),
        ]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                release_identity(
                    {"workflow_run": {**original, key: value}}, "owner/repo"
                )

    def test_receipt_accepts_consistent_pass_and_failure_evidence(self):
        for status in ("PASS", "FAIL"):
            data = {
                "version": "0.6.20",
                "total": 1,
                "pass": int(status == "PASS"),
                "fail": int(status == "FAIL"),
                "rows": [{"status": status}],
            }
            self.assertEqual(validate_receipt(data, "0.6.20"), data)

    def test_receipt_rejects_missing_stale_and_invalid_type_evidence(self):
        for data in (
            {},
            [],
            {"version": "0.7.0"},
            {"version": "0.6.20", "total": True},
        ):
            with self.subTest(data=data), self.assertRaises(ValueError):
                validate_receipt(data, "0.6.20")

    def test_receipt_rejects_empty_truncated_and_inconsistent_counts(self):
        valid = {
            "version": "0.6.20",
            "total": 1,
            "pass": 1,
            "fail": 0,
            "rows": [{"status": "PASS"}],
        }
        for changed in (
            {"total": 0, "pass": 0, "rows": []},
            {"rows": []},
            {"pass": 0},
            {"rows": [{"status": "FAIL"}]},
            {"rows": [{"status": "UNKNOWN"}]},
        ):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                validate_receipt({**valid, **changed}, "0.6.20")

    def run_gate(self, pages, manifest=None):
        run = {
            "repository": {"full_name": "owner/repo"},
            "head_repository": {"full_name": "owner/repo"},
            "event": "push",
            "path": ".github/workflows/release.yml",
            "status": "completed",
            "head_sha": "a" * 40,
            "id": 42,
            "run_attempt": 3,
        }
        manifest = manifest or {
            "encoding": "base64",
            "size": 34,
            "content": base64.b64encode(b'[project]\nversion="0.6.20"\n').decode(),
        }
        urls = []

        def read(request, **kwargs):
            urls.append(request.full_url)
            self.assertEqual(kwargs["timeout"], 15)
            if "/jobs?" in request.full_url:
                data = pages[len(urls) - 1]
            else:
                data = manifest
            return io.BytesIO(json.dumps(data).encode())

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "event.json").write_text(json.dumps({"workflow_run": run}))
            with (
                patch.dict(
                    os.environ,
                    {
                        "GITHUB_EVENT_NAME": "workflow_run",
                        "GITHUB_REPOSITORY": "owner/repo",
                        "GITHUB_EVENT_PATH": str(root / "event.json"),
                        "GITHUB_OUTPUT": str(root / "output"),
                        "GH_TOKEN": "synthetic-test-only",
                    },
                    clear=True,
                ),
                patch("sys.argv", ["gate", "--report", str(root / "receipt.json")]),
                patch("published_cli_release_gate.urlopen", side_effect=read),
                patch("sys.stdout", new_callable=io.StringIO),
            ):
                code = gate_main()
            receipt = json.loads((root / "receipt.json").read_text())
            output = (root / "output").read_text() if (root / "output").exists() else ""
        return code, receipt, output, urls

    def test_full_gate_binds_same_attempt_and_exact_sha_with_later_page_job(self):
        filler = [{"name": "other", "id": n} for n in range(100)]
        job = {
            "name": "Publish to PyPI with Trusted Publishing",
            "id": 99,
            "conclusion": "success",
        }
        code, receipt, output, urls = self.run_gate([{"jobs": filler}, {"jobs": [job]}])
        self.assertEqual(code, 0)
        self.assertEqual(receipt["release_attempt"], 3)
        self.assertEqual(receipt["publication_job_id"], 99)
        self.assertEqual(output, "version=0.6.20\n")
        self.assertIn("runs/42/attempts/3/jobs?per_page=100&page=2", urls[1])
        self.assertTrue(urls[-1].endswith("contents/pyproject.toml?ref=" + "a" * 40))

    def test_full_gate_exhausted_inventory_never_outputs_version(self):
        page = {"jobs": [{"name": "other"}] * 100}
        code, receipt, output, urls = self.run_gate([page] * 10)
        self.assertEqual(code, 1)
        self.assertEqual(receipt["status"], "ERROR")
        self.assertEqual(output, "")
        self.assertEqual(len(urls), 10)

    def test_full_gate_malformed_later_page_never_reads_manifest(self):
        code, receipt, output, urls = self.run_gate(
            [
                {"jobs": [{"name": "other"}] * 100},
                {"jobs": None},
            ]
        )
        self.assertEqual(code, 1)
        self.assertEqual(receipt["status"], "ERROR")
        self.assertEqual(output, "")
        self.assertEqual(len(urls), 2)

    def test_full_gate_oversized_manifest_never_outputs_version(self):
        job = {
            "name": "Publish to PyPI with Trusted Publishing",
            "id": 99,
            "conclusion": "success",
        }
        code, receipt, output, _ = self.run_gate(
            [{"jobs": [job]}],
            {
                "encoding": "base64",
                "size": 65537,
                "content": "",
            },
        )
        self.assertEqual(code, 1)
        self.assertEqual(receipt["status"], "ERROR")
        self.assertEqual(output, "")


if __name__ == "__main__":
    unittest.main()
