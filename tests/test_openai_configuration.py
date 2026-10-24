"""Real CLI, registry and HTTPS adapter with deterministic, socket-free transport.

This suite also runs unchanged against an exact wheel via
``tests/published_cli/openai_configuration.py``.
"""

import io
import json
import ssl
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from review_sensei_github.cli import main as github_main

from review_sensei.cli import main
from review_sensei.providers.openai_compatible import OpenAICompatibleProvider

try:
    from isolated_working_directory import IsolatedWorkingDirectoryMixin
except ModuleNotFoundError:
    from tests.isolated_working_directory import IsolatedWorkingDirectoryMixin

PATCH = (
    "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-x = 0\n+x = 1\n"
)
KEY = "synthetic-openai-configuration-key"
GATEWAY = "https://gateway.example/v1"
OTHER = "https://other.example/v1"


def configuration(endpoint=GATEWAY, permission="true", credential="OPENAI_API_KEY"):
    return (
        "schema: 1\ninference:\n  backend: openai-compatible\n"
        "  model: configured-model\nadvanced:\n  endpoint:\n"
        f"    base_url: {endpoint}\n    allow_custom_endpoint: {permission}\n"
        f"    credential_env: {credential}\n"
    )


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class OpenAIConfigurationTests(IsolatedWorkingDirectoryMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        Path("change.patch").write_text(PATCH, encoding="utf-8", newline="\n")
        self.requests = []
        self.adapters = []
        self.environment = {"OPENAI_API_KEY": KEY}
        self.body = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "summary": "Synthetic clean",
                                "comments": [],
                                "learning_proposals": [],
                            }
                        )
                    }
                }
            ]
        }

    def invoke(self, args):
        def opener(request, timeout, context):
            self.requests.append((request, timeout, context))
            return Response(json.dumps(self.body).encode())

        def adapter(**kwargs):
            provider = OpenAICompatibleProvider(opener=opener, **kwargs)
            self.adapters.append(provider)
            return provider

        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch.dict("os.environ", self.environment, clear=True),
            patch(
                "socket.socket.connect", side_effect=AssertionError("network forbidden")
            ),
            patch(
                "socket.create_connection",
                side_effect=AssertionError("network forbidden"),
            ),
            patch("review_sensei.providers.registry.OpenAICompatibleProvider", adapter),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            entry = github_main if args and args[0] == "host-plan" else main
            status = entry(args)
        self.assertNotIn(KEY, stdout.getvalue() + stderr.getvalue())
        return status, stdout.getvalue(), stderr.getvalue()

    def review(self, extra=()):
        return self.invoke(
            ["--diff", "change.patch", "--no-learning-proposals", *extra]
        )

    def write_config(self, text=None):
        Path(".reviewsensei.yml").write_text(text or configuration(), encoding="utf-8")

    def assert_wire(self, endpoint, model="configured-model"):
        self.assertTrue(self.requests)
        for request, timeout, context in self.requests:
            payload = json.loads(request.data)
            self.assertEqual(
                request.full_url, endpoint.rstrip("/") + "/chat/completions"
            )
            self.assertEqual(request.headers["Authorization"], "Bearer " + KEY)
            self.assertEqual(payload["model"], model)
            self.assertEqual(len(payload["messages"]), 1)
            self.assertEqual(payload["messages"][0]["role"], "user")
            self.assertIsInstance(payload["messages"][0]["content"], str)
            self.assertFalse(payload["stream"])
            self.assertGreater(payload["max_tokens"], 0)
            self.assertEqual(payload["response_format"], {"type": "json_object"})
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(context.check_hostname)
            self.assertGreater(timeout, 0)

    def test_default_endpoint_is_allowlisted_and_environment_cannot_move_it(self):
        self.environment.update(OPENAI_BASE_URL=OTHER, ALLOW_CUSTOM_ENDPOINT="true")
        status, _, error = self.review(["--provider", "openai-compatible"])
        self.assertEqual(status, 0, error)
        self.assert_wire("https://api.openai.com/v1", "gpt-4o-mini")
        self.assertFalse(self.adapters[0].allow_custom_endpoint)

    def test_cli_custom_endpoint_denied_without_explicit_permission(self):
        status, _, error = self.review(
            ["--provider", "openai-compatible", "--base-url", GATEWAY]
        )
        self.assertEqual(status, 2)
        self.assertIn("not allowlisted", error)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.adapters, [])

    def test_cli_custom_endpoint_permission_reaches_real_adapter(self):
        status, _, error = self.review(
            [
                "--provider",
                "openai-compatible",
                "--base-url",
                GATEWAY,
                "--allow-custom-endpoint",
                "--model",
                "cli-model",
            ]
        )
        self.assertEqual(status, 0, error)
        self.assert_wire(GATEWAY, "cli-model")
        self.assertTrue(self.adapters[0].allow_custom_endpoint)

    def test_yaml_only_permission_reaches_real_adapter(self):
        self.write_config()
        status, _, error = self.review()
        self.assertEqual(status, 0, error)
        self.assert_wire(GATEWAY)
        self.assertTrue(self.adapters[0].allow_custom_endpoint)

    def test_explicit_config_path_wins_over_working_directory_config(self):
        self.write_config(configuration(OTHER))
        Path("trusted.yml").write_text(configuration(), encoding="utf-8")
        status, _, error = self.review(["--config", "trusted.yml"])
        self.assertEqual(status, 0, error)
        self.assert_wire(GATEWAY)

    def test_same_endpoint_cli_override_keeps_file_permission(self):
        self.write_config()
        status, _, error = self.review(
            ["--base-url", GATEWAY + "/", "--model", "cli-model"]
        )
        self.assertEqual(status, 0, error)
        self.assert_wire(GATEWAY, "cli-model")

    def test_other_endpoint_cli_override_does_not_inherit_file_permission(self):
        self.write_config()
        status, _, error = self.review(["--base-url", OTHER])
        self.assertEqual(status, 2)
        self.assertIn("not allowlisted", error)
        self.assertEqual(self.requests, [])

    def test_other_endpoint_cli_override_requires_own_permission(self):
        self.write_config()
        status, _, error = self.review(["--base-url", OTHER, "--allow-custom-endpoint"])
        self.assertEqual(status, 0, error)
        self.assert_wire(OTHER)

    def test_permission_without_configured_endpoint_is_not_ambient_authority(self):
        self.write_config(
            "schema: 1\ninference:\n  backend: openai-compatible\nadvanced:\n  endpoint:\n    allow_custom_endpoint: true\n"
        )
        status, _, error = self.review(["--base-url", OTHER])
        self.assertEqual(status, 2)
        self.assertIn("not allowlisted", error)
        self.assertEqual(self.requests, [])

    def test_pr_diff_configuration_cannot_grant_endpoint_authority(self):
        Path("change.patch").write_text(
            "diff --git a/.reviewsensei.yml b/.reviewsensei.yml\n"
            "--- a/.reviewsensei.yml\n+++ b/.reviewsensei.yml\n@@ -1 +1,4 @@\n"
            " schema: 1\n+advanced:\n+  endpoint:\n+    allow_custom_endpoint: true\n",
            encoding="utf-8",
            newline="\n",
        )
        status, _, error = self.review(
            ["--provider", "openai-compatible", "--base-url", OTHER]
        )
        self.assertEqual(status, 2)
        self.assertIn("not allowlisted", error)
        self.assertEqual(self.requests, [])

    def test_cli_and_environment_model_credential_precedence(self):
        self.write_config(configuration(credential="GATEWAY_API_KEY"))
        self.environment = {
            "CLI_API_KEY": KEY,
            "REVIEWSENSEI_MODEL": "environment-model",
        }
        status, _, error = self.review(["--api-key-env", "CLI_API_KEY"])
        self.assertEqual(status, 0, error)
        self.assert_wire(GATEWAY, "environment-model")
        self.requests.clear()
        status, _, error = self.review(
            ["--api-key-env", "CLI_API_KEY", "--model", "cli-model"]
        )
        self.assertEqual(status, 0, error)
        self.assert_wire(GATEWAY, "cli-model")

    def test_openrouter_cannot_use_custom_permission(self):
        self.write_config(
            "schema: 1\ninference:\n  backend: openrouter\n"
            "advanced:\n  endpoint:\n    base_url: https://other.example/api/v1\n"
            "    allow_custom_endpoint: true\n"
        )
        self.environment = {"OPENROUTER_API_KEY": KEY}
        status, _, error = self.review(["--allow-custom-endpoint"])
        self.assertEqual(status, 2)
        self.assertIn("allowlisted", error)
        self.assertEqual(self.requests, [])

    def test_named_profile_ignores_file_authority_and_denies_cli_permission(self):
        self.write_config()
        status, _, error = self.review(["--profile", "fast-triage"])
        self.assertEqual(status, 0, error)
        self.assert_wire("https://api.openai.com/v1", "gpt-4o-mini")
        self.assertFalse(self.adapters[0].allow_custom_endpoint)
        self.requests.clear()
        for options in (["--base-url", GATEWAY], ["--allow-custom-endpoint"]):
            with self.subTest(options=options):
                status, _, _ = self.review(["--profile", "fast-triage", *options])
                self.assertEqual(status, 2)
                self.assertEqual(self.requests, [])

    def test_false_permission_and_malformed_configuration_fail_before_transport(self):
        for text in (
            configuration(permission="false"),
            configuration(permission="yes"),
            "schema: 99\n",
        ):
            with self.subTest(text=text):
                self.write_config(text)
                status, _, _ = self.review()
                self.assertEqual(status, 2)
                self.assertEqual(self.requests, [])
                self.assertEqual(self.adapters, [])

    def test_https_url_boundaries_hold_for_file_and_cli_permission(self):
        for endpoint in (
            "http://127.0.0.1/v1",
            "http://gateway.example/v1",
            "https://user:password@gateway.example/v1",
            GATEWAY + "?key=bad",
            GATEWAY + "#bad",
            "https://gateway.example:bad/v1",
        ):
            with self.subTest(endpoint=endpoint):
                self.write_config(configuration(endpoint))
                status, _, _ = self.review()
                self.assertEqual(status, 2)
                self.assertEqual(self.requests, [])
                Path(".reviewsensei.yml").unlink()
                status, _, _ = self.review(
                    [
                        "--provider",
                        "openai-compatible",
                        "--base-url",
                        endpoint,
                        "--allow-custom-endpoint",
                    ]
                )
                self.assertEqual(status, 2)
                self.assertEqual(self.requests, [])

    def test_custom_credential_selection_and_missing_key(self):
        self.write_config(configuration(credential="GATEWAY_API_KEY"))
        self.environment = {"GATEWAY_API_KEY": KEY}
        status, _, error = self.review()
        self.assertEqual(status, 0, error)
        self.assert_wire(GATEWAY)
        self.requests.clear()
        self.environment = {"OPENAI_API_KEY": KEY}
        status, _, error = self.review()
        self.assertEqual(status, 2)
        self.assertIn("GATEWAY_API_KEY is unavailable", error)
        self.assertEqual(self.requests, [])

    def test_malformed_provider_envelope_is_incomplete_and_redacted(self):
        self.write_config()
        self.body = {"choices": [{"message": {"content": None}}], "private": KEY}
        status, _, error = self.review()
        self.assertEqual(status, 2)
        self.assertIn("did not contain review text", error)
        self.assertTrue(self.requests)

    def test_read_only_commands_report_permission_and_validate_same_endpoint(self):
        self.write_config()
        for command in ("doctor", "plan", "host-plan"):
            with self.subTest(command=command):
                status, output, error = self.invoke([command, "--json"])
                # This offline fixture has no automation ledger or diff.
                # Doctor needs action; plan is incomplete; host-plan is valid.
                self.assertEqual(
                    status, {"doctor": 2, "plan": 3, "host-plan": 0}[command], error
                )
                report = json.loads(output)
                effective = (
                    report
                    if command == "host-plan"
                    else report["provider_configuration"]
                )
                self.assertEqual(effective["base_url"], GATEWAY)
                self.assertTrue(effective["allow_custom_endpoint"])
                self.assertEqual(
                    effective["provenance"]["base_url"]["source"], "configuration file"
                )
                self.assertEqual(self.requests, [])
                self.assertEqual(self.adapters, [])
        for command in ("doctor", "plan"):
            status, output, _ = self.invoke([command, "--base-url", OTHER, "--json"])
            self.assertEqual(status, 2)
            if output:
                self.assertNotIn("provider_configuration", json.loads(output))
            status, output, error = self.invoke(
                [command, "--base-url", OTHER, "--allow-custom-endpoint", "--json"]
            )
            self.assertEqual(status, {"doctor": 2, "plan": 3}[command], error)
            self.assertEqual(
                json.loads(output)["provider_configuration"]["base_url"], OTHER
            )
            provenance = json.loads(output)["provider_configuration"]["provenance"]
            self.assertEqual(
                provenance["base_url"],
                {"source": "command line", "detail": "--base-url"},
            )
            self.assertEqual(
                provenance["allow_custom_endpoint"],
                {"source": "command line", "detail": "--allow-custom-endpoint"},
            )
            status, output, error = self.invoke(
                [command, "--base-url", "https://api.openai.com/v1", "--json"]
            )
            self.assertEqual(status, {"doctor": 2, "plan": 3}[command], error)
            effective = json.loads(output)["provider_configuration"]
            self.assertFalse(effective["allow_custom_endpoint"])
            self.assertEqual(
                effective["provenance"]["allow_custom_endpoint"]["source"],
                "packaged default",
            )
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
