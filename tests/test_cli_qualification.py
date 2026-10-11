"""Regressions for public CLI behavior found in installed 0.6.20 qualification."""

import importlib.metadata
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from review_sensei.cli import _parser, main


class CanonicalDiagnosticsTests(unittest.TestCase):
    def run_command(self, command, config, *, arguments=(), environment=None):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / ".reviewsensei.yml"
            path.write_text(config, encoding="utf-8")
            before = {p.name: p.read_bytes() for p in root.iterdir()}
            stdout, stderr = io.StringIO(), io.StringIO()
            old_cwd = Path.cwd()
            try:
                os.chdir(root)
                with (
                    patch.dict(os.environ, environment or {}, clear=True),
                    patch(
                        "socket.socket.connect",
                        side_effect=AssertionError("offline network"),
                    ),
                    redirect_stdout(stdout),
                    redirect_stderr(stderr),
                ):
                    status = main([command, "--json", *arguments])
            finally:
                os.chdir(old_cwd)
            self.assertEqual({p.name: p.read_bytes() for p in root.iterdir()}, before)
            return status, stdout.getvalue(), stderr.getvalue()

    def test_doctor_and_plan_resolve_cloud_file_configuration(self):
        for command in ("doctor", "plan"):
            with self.subTest(command=command):
                _, output, _ = self.run_command(
                    command,
                    "schema: 1\ninference:\n  backend: cloud-ollama\n  model: glm-5.3:cloud\n",
                )
                effective = json.loads(output)["provider_configuration"]
                self.assertEqual(effective["base_url"], "https://ollama.com/api")
                self.assertEqual(effective["model"], "glm-5.3:cloud")
                self.assertTrue(effective["credential_required"])
                self.assertFalse(effective["credential_present"])

    def test_backend_precedence_matches_review_for_all_public_backends(self):
        backends = ("local-ollama", "cloud-ollama", "openrouter", "openai-compatible")
        for command in ("doctor", "plan"):
            for file_backend in backends:
                for override in backends:
                    with self.subTest(
                        command=command, file=file_backend, override=override
                    ):
                        config = f"schema: 1\ninference:\n  backend: {file_backend}\n"
                        for arguments, environment, expected in (
                            ((), {}, file_backend),
                            ((), {"REVIEWSENSEI_PROVIDER": override}, override),
                            (
                                ("--provider", override),
                                {"REVIEWSENSEI_PROVIDER": file_backend},
                                override,
                            ),
                        ):
                            _, output, _ = self.run_command(
                                command,
                                config,
                                arguments=arguments,
                                environment=environment,
                            )
                            self.assertEqual(
                                json.loads(output)["provider_configuration"]["backend"],
                                expected,
                            )

    def test_model_precedence_and_credential_presence_do_not_render_values(self):
        for command in ("doctor", "plan"):
            _, output, _ = self.run_command(
                command,
                "schema: 1\ninference:\n  backend: cloud-ollama\n  model: glm-5.3:cloud\n",
                arguments=("--model", "deepseek-v4.1-flash:cloud"),
                environment={
                    "REVIEWSENSEI_MODEL": "other:cloud",
                    "OLLAMA_API_KEY": "synthetic-credential-marker",
                },
            )
            effective = json.loads(output)["provider_configuration"]
            self.assertEqual(effective["model"], "deepseek-v4.1-flash:cloud")
            self.assertTrue(effective["credential_present"])
            self.assertNotIn("synthetic-credential-marker", output)

    def test_invalid_schema_and_resource_boundaries_are_rejected_offline(self):
        for command in ("doctor", "plan"):
            for config in (
                "schema: 2\n",
                "schema: 1\nadvanced:\n  resources:\n    timeout_seconds: 0\n",
                "schema: 1\nadvanced:\n  resources:\n    max_provider_calls: 33\n",
            ):
                with self.subTest(command=command, config=config):
                    status, output, error = self.run_command(command, config)
                    self.assertEqual(status, 2)
                    self.assertTrue(output or error)
                    self.assertNotIn("Traceback", output + error)
            for timeout in (1, 3600):
                _, output, _ = self.run_command(
                    command,
                    f"schema: 1\nadvanced:\n  resources:\n    timeout_seconds: {timeout}\n",
                )
                self.assertEqual(
                    json.loads(output)["provider_configuration"]["timeout_seconds"],
                    timeout,
                )

    def test_retired_environment_cannot_override_canonical_defaults(self):
        for command in ("doctor", "plan"):
            _, output, _ = self.run_command(
                command,
                "schema: 1\n",
                environment={
                    "REVIEWSENSEI_PROVIDER_MODE": "cloud",
                    "OLLAMA_MODEL": "retired-model:cloud",
                    "OLLAMA_BASE_URL": "https://retired.example/api",
                },
            )
            effective = json.loads(output)["provider_configuration"]
            self.assertEqual(effective["backend"], "local-ollama")
            self.assertEqual(effective["base_url"], "http://127.0.0.1:11434/api")

    def test_explicit_config_path_and_named_profiles_remain_available(self):
        for command in ("doctor", "plan"):
            _, output, _ = self.run_command(
                command,
                "schema: 1\ninference:\n  backend: cloud-ollama\n",
                arguments=("--config", ".reviewsensei.yml"),
            )
            self.assertEqual(
                json.loads(output)["provider_configuration"]["backend"], "cloud-ollama"
            )
            _, output, _ = self.run_command(
                command,
                "schema: 1\n",
                arguments=("--profile", "local-private", "--provider", "local-ollama"),
            )
            self.assertEqual(
                json.loads(output)["provider_configuration"]["profile"], "local-private"
            )

    def test_invalid_profile_config_and_empty_explicit_values_fail_closed(self):
        for command in ("doctor", "plan"):
            status, _, _ = self.run_command(
                command, "schema: 2\n", arguments=("--profile", "local-private")
            )
            self.assertEqual(status, 2)
            for option in ("--provider", "--model", "--base-url", "--api-key-env"):
                status, _, _ = self.run_command(
                    command, "schema: 1\n", arguments=(option, "")
                )
                self.assertEqual(status, 2)


class EntryPointTests(unittest.TestCase):
    def test_cli_timeout_is_finite_positive_and_preserves_finite_overrides(self):
        for value in ("nan", "inf", "-inf", "1e999", "0", "-1"):
            with self.subTest(value=value), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    _parser().parse_args(["--timeout-seconds", value])
                self.assertEqual(caught.exception.code, 2)
        for value in ("0.5", "1", "3600", "3601"):
            self.assertEqual(
                _parser().parse_args(["--timeout-seconds", value]).timeout_seconds,
                float(value),
            )

    def test_helper_version_does_not_require_action_or_read_hosted_settings(self):
        stdout = io.StringIO()
        with (
            patch.dict(
                os.environ, {"REVIEWSENSEI_OPENROUTER_MODEL": "invalid"}, clear=True
            ),
            redirect_stdout(stdout),
        ):
            with self.assertRaises(SystemExit) as caught:
                main(["resolve-hosted-openrouter", "--version"])
        self.assertEqual(caught.exception.code, 0)
        self.assertEqual(
            stdout.getvalue().strip(), importlib.metadata.version("review-sensei")
        )

    def test_cancellation_unwinds_command_before_returning_130(self):
        events = []

        def interrupted(_):
            try:
                raise KeyboardInterrupt
            finally:
                events.append("cleanup")

        stderr = io.StringIO()
        with (
            patch("review_sensei.cli._run_config_command", side_effect=interrupted),
            redirect_stderr(stderr),
        ):
            status = main(["config"])
        self.assertEqual(status, 130)
        self.assertEqual(events, ["cleanup"])
        self.assertEqual(stderr.getvalue(), "review-sensei: cancelled\n")


if __name__ == "__main__":
    unittest.main()
