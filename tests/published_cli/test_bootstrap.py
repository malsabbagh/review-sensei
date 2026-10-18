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
    HOSTED_MANIFEST,
    clean_environment,
    download_command,
    exact_version,
    propagation_download,
    require_hosted_pin,
    require_installed_distribution,
    require_wheel_digest,
    selected_suites,
    validate_receipt,
    wheel_for,
)


class BootstrapContracts(unittest.TestCase):
    def test_default_suite_set_includes_lifecycle_and_subsets_are_explicit(self):
        self.assertIn("lifecycle", selected_suites(None))
        original = [
            "qualify",
            "transports",
            "seam_boundaries",
            "operations",
            "operation_state",
        ]
        self.assertEqual(selected_suites(original), tuple(original))
        for invalid in ([], ["qualify", "qualify"], ["unknown"]):
            with self.assertRaises(ValueError):
                selected_suites(invalid)

    def test_real_symlink_escape_is_rejected_after_resolution(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            environment = root / "environment"
            environment.mkdir()
            outside = root / "outside/site-packages/review_sensei"
            outside.mkdir(parents=True)
            (outside / "__init__.py").write_text("")
            link = environment / "linked"
            try:
                link.symlink_to(root / "outside", target_is_directory=True)
            except OSError as error:
                self.skipTest("symlink creation unavailable: " + type(error).__name__)
            with self.assertRaises(ValueError):
                require_installed_distribution(
                    {
                        "version": "0.6.21",
                        "origin": str(link / "site-packages/review_sensei/__init__.py"),
                    },
                    "0.6.21",
                    environment,
                )

    def test_missing_empty_or_wrong_type_origin_has_explicit_identity_error(self):
        for identity in [
            None,
            {"version": "0.6.21"},
            {"version": "0.6.21", "origin": None},
            {"version": "0.6.21", "origin": 123},
            {"version": "0.6.21", "origin": ""},
        ]:
            with (
                self.subTest(identity=identity),
                self.assertRaisesRegex(ValueError, "origin"),
            ):
                require_installed_distribution(
                    identity, "0.6.21", Path("/isolated/environment")
                )

    def test_origin_must_be_inside_the_new_environment_not_a_source_checkout(self):
        environment = Path("/isolated/environment")
        valid = {
            "version": "0.6.21",
            "origin": str(
                environment / "lib/python3.14/site-packages/review_sensei/__init__.py"
            ),
        }
        require_installed_distribution(valid, "0.6.21", environment)
        for origin in [
            "/source/review_sensei/__init__.py",
            "/source/site-packages/review_sensei/__init__.py",
            "/isolated/environment-other/lib/site-packages/review_sensei/__init__.py",
            "/isolated/environment/not-site-packages/review_sensei/__init__.py",
        ]:
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                require_installed_distribution(
                    {**valid, "origin": origin}, "0.6.21", environment
                )
        with self.assertRaises(ValueError):
            require_installed_distribution(valid, "0.6.20", environment)

    def test_expected_release_digest_is_optional_but_exact_when_supplied(self):
        require_wheel_digest("a" * 64, None)
        require_wheel_digest("a" * 64, "a" * 64)
        with self.assertRaises(ValueError):
            require_wheel_digest("a" * 64, "b" * 64)

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
        hosted = download_command(
            "python", "0.1.1", Path("wheels"), "review-sensei-github"
        )
        self.assertEqual(hosted[-1], "review-sensei-github==0.1.1")
        for value in [
            "--isolated",
            "--no-deps",
            "--only-binary=:all:",
            "https://pypi.org/simple",
        ]:
            self.assertIn(value, command)
            self.assertIn(value, hosted)

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

    def run_gate(self, pages, manifest=None, hosted=None):
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
        hosted = hosted or (
            b'[project]\nname="review-sensei-github"\nversion="0.1.1"\n'
            b'dependencies=["cryptography>=44,<51","review-sensei==0.6.20"]\n'
        )
        urls = []

        def read(request, **kwargs):
            urls.append(request.full_url)
            self.assertEqual(kwargs["timeout"], 15)
            if "/jobs?" in request.full_url:
                data = pages[len(urls) - 1]
            elif HOSTED_MANIFEST in request.full_url:
                data = {
                    "encoding": "base64",
                    "size": len(hosted),
                    "content": base64.b64encode(hosted).decode(),
                }
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
        self.assertEqual(output, "version=0.6.20\ngithub_version=0.1.1\n")
        self.assertEqual(receipt["github_version"], "0.1.1")
        self.assertIn("runs/42/attempts/3/jobs?per_page=100&page=2", urls[1])
        self.assertTrue(urls[-2].endswith("contents/pyproject.toml?ref=" + "a" * 40))
        self.assertTrue(
            urls[-1].endswith("contents/" + HOSTED_MANIFEST + "?ref=" + "a" * 40)
        )

    def test_full_gate_requires_hosted_manifest_to_pin_the_exact_engine(self):
        job = {
            "name": "Publish to PyPI with Trusted Publishing",
            "id": 99,
            "conclusion": "success",
        }
        for hosted in [
            b'[project]\nname="review-sensei-github"\nversion="0.1.1"\n'
            b'dependencies=["review-sensei==0.6.19"]\n',
            b'[project]\nname="review-sensei-github"\nversion="0.1.1"\n'
            b'dependencies=["review-sensei>=0.6.20"]\n',
            b'[project]\nname="other"\nversion="0.1.1"\n'
            b'dependencies=["review-sensei==0.6.20"]\n',
            b'[project]\nname="review-sensei-github"\nversion="latest"\n'
            b'dependencies=["review-sensei==0.6.20"]\n',
        ]:
            with self.subTest(hosted=hosted):
                code, receipt, output, _ = self.run_gate(
                    [{"jobs": [job]}], hosted=hosted
                )
                self.assertEqual(code, 1)
                self.assertEqual(receipt["status"], "ERROR")
                self.assertEqual(output, "")

    def test_hosted_pin_is_exact_and_unique(self):
        require_hosted_pin(["cryptography<51,>=44", "review-sensei==0.6.23"], "0.6.23")
        for requires in [
            None,
            [],
            ["review-sensei==0.6.22"],
            ["review-sensei>=0.6.23"],
            ["review_sensei==0.6.23", "review-sensei==0.6.23"],
        ]:
            with self.subTest(requires=requires), self.assertRaises(ValueError):
                require_hosted_pin(requires, "0.6.23")

    def test_manual_gate_outputs_both_exact_versions(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            argv = ["--version", "0.6.23", "--github-version", "0.1.1"]
            with (
                patch.dict(
                    os.environ,
                    {
                        "GITHUB_EVENT_NAME": "workflow_dispatch",
                        "GITHUB_OUTPUT": str(root / "output"),
                    },
                    clear=True,
                ),
                patch("sys.argv", ["gate", *argv, "--report", str(root / "r.json")]),
                patch("published_cli_release_gate.urlopen") as urlopen,
                patch("sys.stdout", new_callable=io.StringIO),
            ):
                self.assertEqual(gate_main(), 0)
            urlopen.assert_not_called()
            self.assertEqual(
                (root / "output").read_text(), "version=0.6.23\ngithub_version=0.1.1\n"
            )
            receipt = json.loads((root / "r.json").read_text())
            self.assertEqual(
                (receipt["status"], receipt["mode"], receipt["github_version"]),
                ("PASS", "manual-exact-version", "0.1.1"),
            )

    def test_wheel_selection_matches_exact_name_and_version(self):
        wheels = [
            Path("review_sensei-0.6.23-py3-none-any.whl"),
            Path("review_sensei_github-0.1.1-py3-none-any.whl"),
        ]
        self.assertEqual(wheel_for(wheels, "review-sensei", "0.6.23"), wheels[0])
        self.assertEqual(wheel_for(wheels, "review-sensei-github", "0.1.1"), wheels[1])
        for project, version in [
            ("review-sensei", "0.1.1"),
            ("review-sensei-github", "0.6.23"),
            ("review-sensei", "0.6.2"),
        ]:
            with self.subTest(project=project, version=version):
                with self.assertRaises(RuntimeError):
                    wheel_for(wheels, project, version)

    def test_manual_gate_requires_both_exact_versions(self):
        for argv in [
            ["--version", "0.6.23"],
            ["--github-version", "0.1.1"],
            ["--version", "0.6.23", "--github-version", "latest"],
        ]:
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                with (
                    self.subTest(argv=argv),
                    patch.dict(
                        os.environ,
                        {
                            "GITHUB_EVENT_NAME": "workflow_dispatch",
                            "GITHUB_OUTPUT": str(root / "output"),
                        },
                        clear=True,
                    ),
                    patch(
                        "sys.argv", ["gate", *argv, "--report", str(root / "r.json")]
                    ),
                    patch("sys.stdout", new_callable=io.StringIO),
                ):
                    self.assertEqual(gate_main(), 1)
                    self.assertFalse((root / "output").exists())

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


class LifecycleModuleLoadingContracts(unittest.TestCase):
    def test_contract_files_win_over_same_named_modules_on_the_search_path(self):
        import importlib.util
        from types import ModuleType

        path = Path(__file__).with_name("lifecycle.py")
        spec = importlib.util.spec_from_file_location(
            "published_lifecycle_loader", path
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        names = ("test_approval_lifecycle_contract", "test_local_review_contract")
        previous = dict(sys.modules)
        try:
            with tempfile.TemporaryDirectory() as scratch:
                root = Path(scratch)
                lane = root / "dist_safe"
                lane.mkdir()
                for name in names:
                    (root / (name + ".py")).write_text(
                        'raise RuntimeError("shadow module executed")\n'
                    )
                    (lane / (name + ".py")).write_text(
                        "import unittest\nclass Contract(unittest.TestCase):\n    def test_contract(self): pass\n"
                    )
                    shadow = ModuleType(name)
                    shadow.__file__ = str(root / (name + ".py"))
                    sys.modules[name] = shadow
                with patch.object(sys, "path", [str(root), *sys.path]):
                    suite = module.load_contracts(root)
                    result = unittest.TextTestRunner(stream=io.StringIO()).run(suite)
                self.assertTrue(result.wasSuccessful())
                self.assertEqual(result.testsRun, 2)
                for name in names:
                    self.assertEqual(
                        Path(sys.modules[name].__file__).resolve(),
                        (lane / (name + ".py")).resolve(),
                    )
        finally:
            sys.modules.clear()
            sys.modules.update(previous)

    @unittest.skipIf(os.name == "nt", "symlink creation requires Windows privileges")
    def test_contract_symlink_escape_is_rejected_before_any_module_executes(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "published_lifecycle_loader", Path(__file__).with_name("lifecycle.py")
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            tests = root / "tests"
            lane = tests / "dist_safe"
            lane.mkdir(parents=True)
            marker = root / "executed"
            code = f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
            (lane / "test_approval_lifecycle_contract.py").write_text(code)
            outside = root / "test_local_review_contract.py"
            outside.write_text(code)
            (lane / outside.name).symlink_to(outside)
            with self.assertRaisesRegex(ImportError, "escapes its declared path"):
                module.load_contracts(tests)
            self.assertFalse(marker.exists())
            (lane / outside.name).unlink()
            (lane / "test_approval_lifecycle_contract.py").unlink()
            lane.rmdir()
            lane.symlink_to(root, target_is_directory=True)
            with self.assertRaisesRegex(ImportError, "directory escapes"):
                module.load_contracts(tests)
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
