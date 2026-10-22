"""The postpublication verifier must reject a broken real-consumer contract."""

from __future__ import annotations

import base64
import hashlib
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
consumer = importlib.import_module("verify_npm_consumer")
VERSION = "1.2.3"
SOURCE = "a" * 40
TARGET = "win32-x64" if os.name == "nt" else "darwin-arm64"


class RegistryAndProcessFixture:
    """Replace only registry/process I/O; the complete verifier runs normally."""

    def __init__(self, fault=None):
        self.fault = fault
        self.calls = []
        self.environments = []
        self.lock = None

    def __call__(self, command, *, cwd, env, timeout, stdout, stderr):
        label = stdout.stem
        self.calls.append(label)
        self.environments.append(dict(env))
        stdout.write_text("", encoding="utf-8")
        stderr.write_text("", encoding="utf-8")
        if self.fault == "timeout" and label == "install":
            raise subprocess.TimeoutExpired(command, timeout)
        if self.fault == "install" and label == "install":
            return 1
        output, code = "", 0
        if label == "source":
            output = ("b" * 40 if self.fault == "checkout" else SOURCE) + "\n"
        elif label == "platform":
            output = json.dumps(
                {
                    "platform": "other"
                    if self.fault == "target"
                    else consumer.PLATFORMS[TARGET][0],
                    "arch": consumer.PLATFORMS[TARGET][1],
                    "node": "v22.0.0",
                    "glibc": None,
                }
            )
        elif label == "npm-tooling":
            tooling = Path(command[command.index("--prefix") + 1])
            (tooling / "node_modules/npm/bin").mkdir(parents=True)
            (tooling / "node_modules/npm/bin/npm-cli.js").touch()
        elif label == "npm-version":
            output = consumer.NPM_VERSION + "\n"
        elif label == "install":
            prefix = Path(command[command.index("--prefix") + 1])
            launcher = prefix / "node_modules/@reviewsensei/cli"
            native = prefix / f"node_modules/@reviewsensei/cli-{TARGET}"
            (launcher / "bin").mkdir(parents=True)
            native.mkdir(parents=True)
            (launcher / "bin/review-sensei.js").touch()
            (prefix / "node_modules/.bin").mkdir()
            if os.name == "nt":
                (prefix / "node_modules/.bin/review-sensei.cmd").write_text(
                    '"%_prog%" "%dp0%\\..\\@reviewsensei\\cli\\bin\\review-sensei.js" %*\n'
                )
            else:
                (prefix / "node_modules/.bin/review-sensei").symlink_to(
                    launcher / "bin/review-sensei.js"
                )
            (launcher / "package.json").write_text(
                json.dumps(
                    {
                        "name": "@reviewsensei/cli",
                        "version": VERSION,
                        "bin": {"review-sensei": "bin/review-sensei.js"},
                        "optionalDependencies": {
                            str(t["package"]): VERSION
                            for t in consumer.TARGETS.values()
                        },
                    }
                )
            )
            (native / "package.json").write_text(
                json.dumps(
                    {
                        "name": f"@reviewsensei/cli-{TARGET}",
                        "version": "1.2.4"
                        if self.fault == "native-version"
                        else VERSION,
                    }
                )
            )
            self.lock = {"packages": {}}
            for name in ("@reviewsensei/cli", f"@reviewsensei/cli-{TARGET}"):
                integrity = (
                    "sha512-"
                    + base64.b64encode(hashlib.sha512(name.encode()).digest()).decode()
                )
                self.lock["packages"]["node_modules/" + name] = {
                    "version": VERSION,
                    "integrity": integrity,
                }
            (prefix / "package-lock.json").write_text(json.dumps(self.lock))
        elif label == "version":
            output = "1.2.4" if self.fault == "version" else VERSION
        elif label == "help":
            output = "usage: review-sensei --diff PATCH"
        elif label == "evaluate-fixture":
            report = {
                "passed": self.fault != "evaluation",
                "run": {"mode": "fixture"},
                "threshold_failures": []
                if self.fault != "evaluation"
                else ["exact_fixture_result_rate"],
                "deterministic": {"exact_fixture_result_rate": 1.0},
                "quality": {"false_positive_rate": 0.0},
            }
            Path(command[command.index("--output") + 1]).write_text(json.dumps(report))
        elif label in ("review-required-fix", "review-operational"):
            response = json.loads(
                Path(command[command.index("--fixture-response") + 1]).read_text()
            )
            for comment in response["comments"]:
                comment.pop("side", None)
            if self.fault == "findings":
                response["comments"] = []
            Path(command[command.index("--output") + 1]).write_text(
                json.dumps(response)
            )
            code = 1 if label == "review-required-fix" else 0
        elif label == "invalid-input":
            code = 0 if self.fault == "invalid-exit" else 2
        elif label == "audit-signatures":
            verified = []
            for location, package in self.lock["packages"].items():
                statement = {
                    "predicateType": "https://slsa.dev/provenance/v1",
                    "subject": [
                        {
                            "digest": {
                                "sha512": base64.b64decode(
                                    package["integrity"][7:]
                                ).hex()
                            }
                        }
                    ],
                    "predicate": {
                        "buildDefinition": {
                            "externalParameters": {
                                "workflow": {
                                    "ref": f"refs/tags/v{VERSION}",
                                    "repository": consumer.REPOSITORY,
                                    "path": ".github/workflows/release.yml"
                                    if self.fault == "workflow"
                                    else ".github/workflows/publish-npm.yml",
                                }
                            },
                            "resolvedDependencies": [
                                {
                                    "uri": f"git+{consumer.REPOSITORY}@refs/tags/v{VERSION}",
                                    "digest": {
                                        "gitCommit": "b" * 40
                                        if self.fault == "provenance"
                                        else SOURCE
                                    },
                                }
                            ],
                        }
                    },
                }
                verified.append(
                    {
                        "name": location.removeprefix("node_modules/"),
                        "location": location,
                        "version": VERSION,
                        "attestationBundles": [
                            {
                                "bundle": {
                                    "dsseEnvelope": {
                                        "payload": base64.b64encode(
                                            json.dumps(statement).encode()
                                        ).decode()
                                    }
                                }
                            }
                        ],
                    }
                )
            output = json.dumps(
                {
                    "invalid": [],
                    "missing": ["attestation"] if self.fault == "audit" else [],
                    "verified": verified,
                }
            )
        stdout.write_text(output, encoding="utf-8")
        return code


class NpmConsumerTests(unittest.TestCase):
    def verify(self, directory, fixture):
        root = directory / "source"
        root.mkdir()
        (root / "pyproject.toml").write_text('[project]\nversion="1.2.3"\n')
        shutil.copytree(
            ROOT / "tests/fixtures/standalone-smoke",
            root / "tests/fixtures/standalone-smoke",
        )
        shutil.copytree(ROOT / "evaluation/v1", root / "evaluation/v1")
        node, npm = directory / "node", directory / "npm-cli.js"
        node.touch()
        npm.touch()
        return consumer.verify_consumer(
            root=root,
            version=VERSION,
            source_sha=SOURCE,
            target=TARGET,
            receipt_dir=directory / "receipt",
            node=node,
            npm_cli=npm,
            runner=fixture,
        )

    def test_exact_public_consumer_contract_and_safe_receipts(self):
        fixture = RegistryAndProcessFixture()
        with tempfile.TemporaryDirectory() as scratch:
            directory = Path(scratch)
            with patch.dict(
                os.environ,
                {
                    "GITHUB_TOKEN": "private",
                    "NPM_TOKEN": "private",
                    "OPENAI_API_KEY": "private",
                    "PYTHONPATH": "private",
                },
            ):
                receipt = self.verify(directory, fixture)
            self.assertTrue(receipt["successful"])
            self.assertEqual(
                [x["exit_code"] for x in receipt["steps"]],
                [0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 2, 0],
            )
            self.assertTrue((directory / "receipt/package-lock.json").is_file())
            self.assertFalse((directory / "receipt/prefix").exists())
            for environment in fixture.environments:
                self.assertEqual(environment["PATH"], str(directory.resolve()))
                for name in (
                    "GITHUB_TOKEN",
                    "NPM_TOKEN",
                    "OPENAI_API_KEY",
                    "PYTHONPATH",
                ):
                    self.assertNotIn(name, environment)
            saved = json.loads((directory / "receipt/receipt.json").read_text())
            self.assertTrue(saved["successful"])
            self.assertEqual(saved["source_sha"], SOURCE)

    def test_broken_installed_behavior_or_provenance_never_reports_success(self):
        for fault in (
            "checkout",
            "target",
            "install",
            "native-version",
            "version",
            "evaluation",
            "findings",
            "invalid-exit",
            "audit",
            "provenance",
            "workflow",
            "timeout",
        ):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as scratch:
                directory = Path(scratch)
                fixture = RegistryAndProcessFixture(fault)
                receipt = self.verify(directory, fixture)
                self.assertFalse(receipt["successful"])
                self.assertIn("failure_type", receipt)
                saved = json.loads((directory / "receipt/receipt.json").read_text())
                self.assertFalse(saved["successful"])
                self.assertTrue(list((directory / "receipt").glob("*.stdout")))
                if fault == "checkout":
                    self.assertEqual(fixture.calls, ["source"])
                if fault in (
                    "native-version",
                    "version",
                    "evaluation",
                    "findings",
                    "invalid-exit",
                    "audit",
                    "provenance",
                ):
                    self.assertTrue((directory / "receipt/package-lock.json").is_file())

    def test_failure_receipt_hash_matches_final_retained_lock(self):
        for failed_step in ("version", "audit-signatures"):
            with (
                self.subTest(failed_step=failed_step),
                tempfile.TemporaryDirectory() as scratch,
            ):
                directory = Path(scratch)
                fixture = RegistryAndProcessFixture()
                changed_lock = b'{"packages": {}, "changed_after_install": true}\n'

                def runner(command, **kwargs):
                    code = fixture(command, **kwargs)
                    if kwargs["stdout"].stem == failed_step:
                        (directory / "receipt/prefix/package-lock.json").write_bytes(
                            changed_lock
                        )
                        return 1
                    return code

                receipt = self.verify(directory, runner)
                retained = directory / "receipt/package-lock.json"
                self.assertFalse(receipt["successful"])
                self.assertEqual(retained.read_bytes(), changed_lock)
                self.assertEqual(
                    receipt["package_lock_sha256"],
                    hashlib.sha256(retained.read_bytes()).hexdigest(),
                )
                self.assertEqual(
                    json.loads((directory / "receipt/receipt.json").read_text())[
                        "package_lock_sha256"
                    ],
                    receipt["package_lock_sha256"],
                )

    def test_missing_or_oversized_final_lock_never_gets_an_unrelated_hash(self):
        for state in ("missing-prefix", "oversized-prefix", "missing", "oversized"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as scratch:
                directory = Path(scratch)
                fixture = RegistryAndProcessFixture()

                def runner(command, **kwargs):
                    code = fixture(command, **kwargs)
                    if kwargs["stdout"].stem == "version":
                        prefix_lock = directory / "receipt/prefix/package-lock.json"
                        prefix_lock.unlink()
                        if state == "oversized-prefix":
                            prefix_lock.write_bytes(
                                b"x" * (consumer.MAX_OUTPUT_BYTES + 1)
                            )
                        retained = directory / "receipt/package-lock.json"
                        if state == "missing":
                            retained.unlink()
                        elif state == "oversized":
                            retained.write_bytes(b"x" * (consumer.MAX_OUTPUT_BYTES + 1))
                        return 1
                    return code

                receipt = self.verify(directory, runner)
                retained = directory / "receipt/package-lock.json"
                self.assertFalse(receipt["successful"])
                if state in ("missing", "oversized"):
                    self.assertNotIn("package_lock_sha256", receipt)
                else:
                    self.assertEqual(retained.read_text(), json.dumps(fixture.lock))
                    self.assertEqual(
                        receipt["package_lock_sha256"],
                        hashlib.sha256(retained.read_bytes()).hexdigest(),
                    )

    def test_real_process_oversized_logs_are_capped_after_completion_or_timeout(self):
        for timed_out in (False, True):
            with (
                self.subTest(timed_out=timed_out),
                tempfile.TemporaryDirectory() as scratch,
            ):
                directory = Path(scratch)
                fixture = RegistryAndProcessFixture()
                size = consumer.MAX_OUTPUT_BYTES + 1

                def runner(command, **kwargs):
                    if kwargs["stdout"].stem != "source":
                        return fixture(command, **kwargs)
                    command = [
                        sys.executable,
                        "-c",
                        "import sys,time;"
                        f"sys.stdout.buffer.write(b'x'*{size});sys.stdout.flush();"
                        f"sys.stderr.buffer.write(b'y'*{size});sys.stderr.flush();"
                        + ("time.sleep(30)" if timed_out else ""),
                    ]
                    return consumer.run_command(
                        command, **{**kwargs, "timeout": 5 if timed_out else 10}
                    )

                receipt = self.verify(directory, runner)
                self.assertFalse(receipt["successful"])
                self.assertEqual(
                    receipt["failure_type"],
                    "TimeoutExpired" if timed_out else "ConsumerVerificationError",
                )
                if not timed_out:
                    self.assertIn(
                        "output exceeds the receipt bound", receipt["failure"]
                    )
                source = receipt["steps"][0]
                for stream in ("stdout", "stderr"):
                    self.assertEqual(
                        (directory / "receipt" / source[stream]).stat().st_size,
                        consumer.MAX_OUTPUT_BYTES,
                    )
                    self.assertEqual(source[f"{stream}_bytes"], size)
                    self.assertIs(source[f"{stream}_truncated"], True)
                saved = json.loads((directory / "receipt/receipt.json").read_text())
                self.assertEqual(saved["steps"], receipt["steps"])
                self.assertFalse(saved["successful"])

    def test_missing_node_keeps_failure_receipt(self):
        with tempfile.TemporaryDirectory() as scratch:
            directory = Path(scratch) / "receipt"
            with (
                patch.object(
                    sys,
                    "argv",
                    [
                        "verify_npm_consumer.py",
                        "--version",
                        VERSION,
                        "--source-sha",
                        SOURCE,
                        "--target",
                        "darwin-arm64",
                        "--receipt-dir",
                        str(directory),
                    ],
                ),
                patch.object(consumer.shutil, "which", return_value=None),
            ):
                self.assertEqual(consumer.main(), 1)
            self.assertFalse(
                json.loads((directory / "receipt.json").read_text())["successful"]
            )

    def test_windows_bootstrap_and_command_ownership_do_not_require_shell_execution(
        self,
    ):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            node = root / "node.exe"
            node.touch()
            npm = root / "node_modules/npm/bin/npm-cli.js"
            npm.parent.mkdir(parents=True)
            npm.touch()
            with patch.object(
                consumer.shutil, "which", return_value=str(root / "npm.cmd")
            ):
                self.assertEqual(consumer.npm_cli_path(node), npm)
            launcher = (
                root / "prefix/node_modules/@reviewsensei/cli/bin/review-sensei.js"
            )
            shim = root / "prefix/node_modules/.bin/review-sensei.cmd"
            shim.parent.mkdir(parents=True)
            shim.write_text(
                '"%_prog%" "%dp0%\\..\\@reviewsensei\\cli\\bin\\review-sensei.js" %*\n'
            )
            self.assertEqual(
                consumer.launcher_command(root / "prefix", launcher, node, "win32-x64"),
                [str(node), str(launcher)],
            )
            shim.write_text('"%_prog%" "%dp0%\\..\\another-package\\cli.js" %*\n')
            with self.assertRaises(consumer.ConsumerVerificationError):
                consumer.launcher_command(root / "prefix", launcher, node, "win32-x64")

    def test_matrix_runs_after_publication_with_readonly_authority_and_failure_artifacts(
        self,
    ):
        workflow = (ROOT / ".github/workflows/publish-npm.yml").read_text()
        verification = workflow.split("  verify-published:\n", 1)[1].split(
            "\n  channel-result:", 1
        )[0]
        self.assertIn("needs: [qualify-source, publish-npm]", verification)
        self.assertIn("timeout-minutes: 20", verification)
        self.assertIn("contents: read", verification)
        for forbidden in (
            "id-token",
            "secrets.",
            "NPM_TOKEN",
            "environment:",
            "pip install",
            "npm publish",
        ):
            self.assertNotIn(forbidden, verification)
        self.assertIn("if: ${{ always() }}", verification)
        self.assertIn("if-no-files-found: error", verification)
        self.assertIn("needs.qualify-source.outputs.version", verification)
        self.assertIn("needs.qualify-source.outputs.source_sha", verification)
        for target in consumer.TARGETS:
            self.assertIn("target: " + target, verification)
        self.assertIn(
            "publish-npm, verify-published]",
            workflow.split("  channel-result:\n", 1)[1],
        )


if __name__ == "__main__":
    unittest.main()
