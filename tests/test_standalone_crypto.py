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
        for target in INSTALLER.TARGETS:
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
            "archive_types": {"PYZ.pyz": "z", "_ssl.so": "b"},
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

    def test_collected_data_names_do_not_create_forbidden_packages(self):
        for name in (
            "_internal/cryptography.txt",
            "_internal/some.cryptography",
            "_internal/cryptography.notes.py",
            "_internal/review_sensei_github.notes.pyc",
            "_internal/docs/cryptography/README.txt",
            "_internal/docs/review_sensei_github/example.py",
        ):
            path = self.bundle / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("ordinary data")
            with self.subTest(name=name):
                report = CHECKER.check_bundle(self.bundle, run=self.run_probe)
                self.assertEqual(report["check"], "passed")
            path.unlink()

    def test_archive_data_names_do_not_create_forbidden_modules(self):
        for name in (
            "cryptography.txt",
            "some.cryptography",
            "docs/cryptography/README.txt",
        ):
            self.inventory["archive_entries"].append(name)
            self.inventory["archive_types"][name] = "x"
            with self.subTest(name=name):
                report = CHECKER.check_bundle(self.bundle, run=self.run_probe)
                self.assertEqual(report["check"], "passed")
            self.inventory["archive_entries"].pop()
            self.inventory["archive_types"].pop(name)

    def test_schema_version_requires_an_integer(self):
        for version in (True, 1.0):
            with (
                self.subTest(version=version),
                self.assertRaisesRegex(
                    CHECKER.StandaloneCryptoError, "inventory is invalid"
                ),
            ):
                CHECKER.check_bundle(
                    self.bundle,
                    run=lambda *a, version=version, **kw: subprocess.CompletedProcess(
                        a[0],
                        0,
                        json.dumps({**self.inventory, "schema_version": version}),
                    ),
                )

    def test_missing_bundle_fails_before_probe(self):
        with self.assertRaisesRegex(CHECKER.StandaloneCryptoError, "bundle is missing"):
            CHECKER.check_bundle(
                self.bundle / "missing", run=lambda *a, **kw: self.fail("probe entered")
            )

    def test_missing_executable_fails_before_probe(self):
        self.executable.unlink()
        with self.assertRaisesRegex(
            CHECKER.StandaloneCryptoError, "executable is missing"
        ):
            CHECKER.check_bundle(
                self.bundle, run=lambda *a, **kw: self.fail("probe entered")
            )

    def test_loose_module_and_namespace_package_files_fail_before_probe(self):
        for name in (
            "cryptography.py",
            "review_sensei_github.pyc",
            "_internal/cryptography" + CHECKER.EXTENSION_SUFFIXES[0],
            "_internal/review_sensei_github" + CHECKER.EXTENSION_SUFFIXES[-1],
            "_internal/cryptography/namespace_data.txt",
            "review_sensei_github/namespace_data.txt",
        ):
            path = self.bundle / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(CHECKER.StandaloneCryptoError, "forbidden"),
            ):
                CHECKER.check_bundle(
                    self.bundle, run=lambda *a, **kw: self.fail("probe entered")
                )
            path.unlink()

    def test_importable_package_directory_alias_fails_before_probe(self):
        internal = self.bundle / "_internal"
        internal.mkdir()
        runtime = internal / "synthetic_runtime"
        runtime.mkdir()
        (runtime / "__init__.py").touch()
        for package in ("cryptography", "review_sensei_github"):
            alias = internal / package
            try:
                alias.symlink_to(runtime, target_is_directory=True)
            except OSError as exc:
                self.skipTest("directory symlinks unavailable: " + str(exc))
            with (
                self.subTest(package=package),
                self.assertRaisesRegex(CHECKER.StandaloneCryptoError, "forbidden"),
            ):
                CHECKER.check_bundle(
                    self.bundle, run=lambda *a, **kw: self.fail("probe entered")
                )
            alias.unlink()

    def test_internal_directory_alias_cannot_hide_loose_module(self):
        runtime = self.bundle / "synthetic_runtime"
        runtime.mkdir()
        (runtime / "cryptography.py").touch()
        try:
            (self.bundle / "_internal").symlink_to(runtime, target_is_directory=True)
        except OSError as exc:
            self.skipTest("directory symlinks unavailable: " + str(exc))
        with self.assertRaisesRegex(CHECKER.StandaloneCryptoError, "forbidden"):
            CHECKER.check_bundle(
                self.bundle, run=lambda *a, **kw: self.fail("probe entered")
            )

    def test_archive_code_item_kinds_are_checked_as_module_names(self):
        for kind in ("m", "M", "s"):
            for name in ("cryptography", "review_sensei_github.commands"):
                self.inventory["archive_entries"].append(name)
                self.inventory["archive_types"][name] = kind
                with (
                    self.subTest(kind=kind, name=name),
                    self.assertRaisesRegex(CHECKER.StandaloneCryptoError, "forbidden"),
                ):
                    CHECKER.check_bundle(self.bundle, run=self.run_probe)
                self.inventory["archive_entries"].pop()
                self.inventory["archive_types"].pop(name)

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
            forbidden.parent.rmdir()

    def test_pyz_only_forbidden_modules_and_archive_entries_fail(self):
        for field in ("python_modules", "archive_entries"):
            for name in (
                "cryptography",
                "cryptography.hazmat.bindings._rust",
                "review_sensei_github",
                "review_sensei_github.commands",
            ):
                self.inventory[field].append(name)
                if field == "archive_entries":
                    self.inventory["archive_types"][name] = "m"
                with (
                    self.subTest(field=field, name=name),
                    self.assertRaisesRegex(CHECKER.StandaloneCryptoError, "forbidden"),
                ):
                    CHECKER.check_bundle(self.bundle, run=self.run_probe)
                self.inventory[field].pop()
                if field == "archive_entries":
                    self.inventory["archive_types"].pop(name)

    def test_missing_malformed_or_empty_inventory_fails(self):
        for output in (
            "",
            "not json",
            "{}",
            "[]",
            "null",
            "1",
            "[[]]",
            json.dumps({**self.inventory, "pyinstaller": "6.17.0"}),
            json.dumps({**self.inventory, "pyinstaller": None}),
            json.dumps({**self.inventory, "python_modules": []}),
            json.dumps({**self.inventory, "pyz_archives": 0}),
            json.dumps({**self.inventory, "python_modules": ["ssl"]}),
            json.dumps({**self.inventory, "archive_entries": [None]}),
            json.dumps({**self.inventory, "python_modules": [["review_sensei"]]}),
            json.dumps({**self.inventory, "archive_types": {}}),
            json.dumps({**self.inventory, "archive_types": []}),
            json.dumps(
                {**self.inventory, "archive_types": {"PYZ.pyz": "?", "_ssl.so": "b"}}
            ),
            json.dumps(
                {**self.inventory, "archive_types": {"PYZ.pyz": [], "_ssl.so": "b"}}
            ),
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
