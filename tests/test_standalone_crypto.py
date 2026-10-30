from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    with patch.object(sys, "path", [str(ROOT / "scripts"), *sys.path]):
        spec.loader.exec_module(module)
    return module


INSTALLER = load_script("install_standalone_oracle")
CHECKER = load_script("check_standalone_crypto")


class OracleTests(unittest.TestCase):
    def test_all_targets_install_only_core_with_no_homebrew_requirement(self):
        for target in ("darwin-x64", "darwin-arm64", "win32-x64"):
            with self.subTest(target=target):
                calls = []

                def run(command, **kwargs):
                    if command[0] == "brew":
                        raise FileNotFoundError("Homebrew unavailable")
                    calls.append((command, kwargs))

                with patch.object(INSTALLER, "validate_host_target"):
                    INSTALLER.install_oracle(
                        ROOT, target, environment={"SYNTHETIC": "kept"}, run=run
                    )
                self.assertEqual(len(calls), 1)
                command, kwargs = calls[0]
                self.assertEqual(command[-1], ".")
                for flag in ("--no-cache-dir", "--no-binary", "--force-reinstall"):
                    self.assertNotIn(flag, command)
                self.assertEqual(kwargs["env"], {"SYNTHETIC": "kept"})
                self.assertEqual(kwargs["cwd"], ROOT)

    def test_installation_errors_still_fail(self):
        for failure in (
            FileNotFoundError("pip missing"),
            subprocess.CalledProcessError(1, ["pip"]),
        ):
            with (
                self.subTest(failure=type(failure).__name__),
                patch.object(INSTALLER, "validate_host_target"),
            ):
                with self.assertRaisesRegex(
                    INSTALLER.OracleInstallError, "installation failed"
                ):
                    INSTALLER.install_oracle(
                        ROOT,
                        "darwin-x64",
                        run=lambda *args, **kwargs: (_ for _ in ()).throw(failure),
                    )


class EngineBundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.bundle = Path(self.temp.name)
        self.executable = self.bundle / "review-sensei"
        self.executable.touch()
        self.executable.chmod(0o755)
        self.inventory = {
            "schema_version": 1,
            "pyinstaller": "6.16.0",
            "archive_entries": ["PYZ.pyz", "_ssl.so"],
            "python_modules": [
                "review_sensei",
                "review_sensei.cli",
                "review_sensei.hosting.github.trigger",
                "ssl",
            ],
            "pyz_archives": 1,
        }

    def run_probe(self, command, **kwargs):
        self.assertEqual(command[-1], str(self.executable.resolve()))
        self.assertEqual(command[1:3], ["-I", "-c"])
        self.assertIn("CArchiveReader", command[3])
        self.assertIn("open_embedded_archive", command[3])
        self.assertEqual(kwargs["timeout"], 30)
        return subprocess.CompletedProcess(command, 0, json.dumps(self.inventory))

    def test_engine_only_bundle_and_normal_tls_pass(self):
        internal = self.bundle / "_internal"
        internal.mkdir()
        (internal / "libssl.3.dylib").touch()
        (internal / "libcrypto.3.dylib").touch()
        report = CHECKER.check_bundle(self.bundle, run=self.run_probe)
        self.assertEqual(report["check"], "passed")
        self.assertEqual(report["boundary"], "engine-only")
        self.assertEqual(report["python_modules"], 4)
        self.assertEqual(len(report["inventory_sha256"]), 64)

    def test_missing_bundle_or_executable_fails(self):
        for bundle in (self.bundle / "missing", self.bundle):
            self.executable.unlink(missing_ok=True)
            with (
                self.subTest(bundle=bundle),
                self.assertRaises(CHECKER.StandaloneCryptoError),
            ):
                CHECKER.check_bundle(
                    bundle, run=lambda *a, **kw: self.fail("probe entered")
                )

    def test_collected_forbidden_packages_fail_before_probe(self):
        for package in ("cryptography", "review_sensei_github"):
            forbidden = self.bundle / "_internal" / package / "__init__.pyc"
            forbidden.parent.mkdir(parents=True)
            forbidden.touch()
            with (
                self.subTest(package=package),
                self.assertRaisesRegex(CHECKER.StandaloneCryptoError, "forbidden"),
            ):
                CHECKER.check_bundle(
                    self.bundle, run=lambda *a, **kw: self.fail("probe entered")
                )
            forbidden.unlink()

    def test_pyz_only_forbidden_modules_and_archive_entries_fail(self):
        for field in ("python_modules", "archive_entries"):
            for name in (
                "cryptography",
                "cryptography.hazmat.bindings._rust",
                "review_sensei_github.commands",
            ):
                self.inventory[field].append(name)
                with (
                    self.subTest(field=field, name=name),
                    self.assertRaisesRegex(CHECKER.StandaloneCryptoError, "forbidden"),
                ):
                    CHECKER.check_bundle(self.bundle, run=self.run_probe)
                self.inventory[field].pop()

    def test_missing_malformed_or_empty_inventory_fails(self):
        for output in (
            "",
            "not json",
            "{}",
            "[]",
            json.dumps({**self.inventory, "python_modules": []}),
            json.dumps({**self.inventory, "pyz_archives": 0}),
            json.dumps({**self.inventory, "python_modules": ["ssl"]}),
            json.dumps({**self.inventory, "archive_entries": [None]}),
        ):
            with (
                self.subTest(output=output),
                self.assertRaisesRegex(
                    CHECKER.StandaloneCryptoError, "inventory is invalid"
                ),
            ):
                CHECKER.check_bundle(
                    self.bundle,
                    run=lambda command, **kw: subprocess.CompletedProcess(
                        command, 0, output
                    ),
                )

    def test_inventory_failure_is_bounded_and_redacts_credentials(self):
        def run(command, **kwargs):
            raise subprocess.CalledProcessError(
                1, command, stderr="invalid archive synthetic-credential" + "x" * 5000
            )

        with patch.dict("os.environ", {"SYNTHETIC_API_KEY": "synthetic-credential"}):
            with self.assertRaisesRegex(
                CHECKER.StandaloneCryptoError, "invalid archive"
            ) as raised:
                CHECKER.check_bundle(self.bundle, run=run)
        self.assertNotIn("synthetic-credential", str(raised.exception))
        self.assertLess(len(str(raised.exception)), 4200)


if __name__ == "__main__":
    unittest.main()
