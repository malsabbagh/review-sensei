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
    def test_intel_source_build_uses_static_archives_and_cannot_reuse_shared_wheel(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp)
            (prefix / "lib").mkdir()
            for name in ("libssl.a", "libcrypto.a"):
                (prefix / "lib" / name).touch()
            calls = []

            def run(command, **kwargs):
                calls.append((command, kwargs))
                return subprocess.CompletedProcess(command, 0, str(prefix) + "\n")

            with patch.object(INSTALLER, "validate_host_target"):
                INSTALLER.install_oracle(
                    ROOT,
                    "darwin-x64",
                    environment={"OPENSSL_STATIC": "0"},
                    run=run,
                )
            command, kwargs = calls[1]
            self.assertIn("--no-cache-dir", command)
            self.assertEqual(command[command.index("--no-binary") + 1], "cryptography")
            self.assertIn("--force-reinstall", command)
            self.assertEqual(kwargs["env"]["OPENSSL_STATIC"], "1")
            self.assertEqual(kwargs["env"]["OPENSSL_DIR"], str(prefix))
            self.assertEqual(kwargs["cwd"], ROOT)

    def test_missing_static_archive_fails_before_installing(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []

            def run(command, **kwargs):
                calls.append(command)
                return subprocess.CompletedProcess(command, 0, tmp)

            with patch.object(INSTALLER, "validate_host_target"):
                with self.assertRaisesRegex(INSTALLER.OracleInstallError, "archives"):
                    INSTALLER.install_oracle(ROOT, "darwin-x64", run=run)
            self.assertEqual(calls, [["brew", "--prefix", "openssl@3"]])

    def test_other_targets_preserve_default_dependency_install(self):
        for target in ("darwin-arm64", "win32-x64"):
            with self.subTest(target=target):
                calls = []
                with patch.object(INSTALLER, "validate_host_target"):
                    INSTALLER.install_oracle(
                        ROOT,
                        target,
                        environment={"SYNTHETIC": "kept"},
                        run=lambda command, **kwargs: calls.append((command, kwargs)),
                    )
                self.assertEqual(len(calls), 1)
                command, kwargs = calls[0]
                self.assertEqual(command[-1], ".")
                self.assertNotIn("--no-binary", command)
                self.assertEqual(kwargs["env"], {"SYNTHETIC": "kept"})


class FrozenBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.bundle = Path(self.temp.name)
        directory = self.bundle / "_internal/cryptography/hazmat/bindings"
        directory.mkdir(parents=True)
        self.binding = directory / "_rust.abi3.so"
        self.binding.touch()

    def test_dynamic_openssl_is_rejected_before_the_probe_can_mask_the_abi_failure(
        self,
    ):
        for library in ("libssl.3.dylib", "libcrypto.3.dylib"):
            with self.subTest(library=library):
                calls = []

                def run(command, **kwargs):
                    calls.append(command)
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        f"binding:\n\t@rpath/{library} (compatibility version 3.0.0)\n",
                    )

                with self.assertRaisesRegex(
                    CHECKER.StandaloneCryptoError, "shared OpenSSL"
                ):
                    CHECKER.check_bundle(self.bundle, run=run)
                self.assertEqual(len(calls), 1)

    def test_signing_probe_must_use_the_rewritten_frozen_binding(self):
        calls = []

        def run(command, **kwargs):
            calls.append((command, kwargs))
            if len(calls) == 1:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    "binding:\n\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0)\n",
                )
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps(
                    {
                        "binding": str(self.binding.resolve()),
                        "rsa_verified": True,
                        "openssl": "synthetic static OpenSSL",
                    }
                ),
            )

        result = CHECKER.check_bundle(self.bundle, run=run)
        self.assertEqual(result["check"], "passed")
        self.assertEqual(result["shared_openssl_dependencies"], [])
        self.assertEqual(calls[1][0][-1], str(self.binding.resolve()))
        self.assertEqual(calls[1][1]["timeout"], 30)
        self.assertIn("key.public_key().verify", calls[1][0][-2])

    def test_oracle_binding_cannot_be_substituted_for_the_frozen_file(self):
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(
                command,
                0,
                "binding:\n"
                if len(calls) == 1
                else json.dumps(
                    {
                        "binding": "/some/installed/oracle/_rust.so",
                        "rsa_verified": True,
                        "openssl": "unverified",
                    }
                ),
            )

        with self.assertRaisesRegex(CHECKER.StandaloneCryptoError, "result is invalid"):
            CHECKER.check_bundle(self.bundle, run=run)

    def test_ambiguous_binding_fails_before_any_native_process(self):
        self.binding.with_name("_rust.other.so").touch()
        with self.assertRaisesRegex(CHECKER.StandaloneCryptoError, "one frozen"):
            CHECKER.find_binding(self.bundle)

    def test_probe_failure_retains_bounded_loader_cause_without_credentials(self):
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            if len(calls) == 1:
                return subprocess.CompletedProcess(command, 0, "binding:\n")
            raise subprocess.CalledProcessError(
                1, command, stderr="Symbol not found: _SSL_missing synthetic-credential"
            )

        with patch.dict("os.environ", {"SYNTHETIC_API_KEY": "synthetic-credential"}):
            with self.assertRaisesRegex(
                CHECKER.StandaloneCryptoError, "Symbol not found: _SSL_missing"
            ) as raised:
                CHECKER.check_bundle(self.bundle, run=run)
        self.assertNotIn("synthetic-credential", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
