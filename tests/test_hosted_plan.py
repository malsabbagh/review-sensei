import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from review_sensei.cli import main
from review_sensei.configuration import (
    BACKEND_DEFAULTS,
    ConfigurationError,
    parse_configuration_text,
)
from review_sensei.hosted import (
    RUNNER_KINDS,
    hosted_plan_outputs,
    load_and_plan_hosted,
    plan_hosted_execution,
    render_github_outputs,
    render_hosted_plan,
    write_github_outputs,
)

ACTIVATED = """schema: 1

inference:
  backend: cloud-ollama
  model: deepseek-v4.1-flash:cloud

github:
  writes: true
  reviews: auto-approve
"""

LOCAL = """schema: 1

inference:
  backend: local-ollama
  model: qwen3.5:4b
"""


def configuration(text, source=".reviewsensei.yml"):
    return parse_configuration_text(text, source=source, root=Path("/tmp"))


class HostedPlanResolutionTests(unittest.TestCase):
    def test_packaged_defaults_select_the_local_runner(self):
        plan = plan_hosted_execution(configuration("schema: 1\n"), environ={})
        self.assertEqual(plan.backend, "local-ollama")
        self.assertEqual(plan.model, "qwen3.5:4b")
        self.assertEqual(plan.runner_kind, "local")
        self.assertEqual(plan.credential_env, "OLLAMA_API_KEY")
        self.assertFalse(plan.credential_required)
        self.assertEqual(plan.source_of("backend").source, "packaged default")

    def test_declared_configuration_selects_the_hosted_runner_and_policy(self):
        plan = plan_hosted_execution(configuration(ACTIVATED), environ={})
        self.assertEqual(plan.backend, "cloud-ollama")
        self.assertEqual(plan.model, "deepseek-v4.1-flash:cloud")
        self.assertEqual(plan.runner_kind, "hosted")
        self.assertEqual(plan.credential_env, "OLLAMA_API_KEY")
        self.assertTrue(plan.credential_required)
        self.assertEqual(plan.policy.writes, True)
        self.assertEqual(plan.policy.reviews, "auto-approve")
        self.assertEqual(plan.policy.automatic_reviews, True)
        self.assertEqual(plan.policy.learning, "disabled")
        self.assertEqual(plan.policy.artifacts, "none")

    def test_model_only_override_keeps_the_declared_backend(self):
        plan = plan_hosted_execution(
            configuration(LOCAL),
            environ={"REVIEWSENSEI_MODEL": "qwen3.5:8b"},
        )
        self.assertEqual(plan.backend, "local-ollama")
        self.assertEqual(plan.model, "qwen3.5:8b")
        self.assertEqual(plan.source_of("model").source, "environment variable")

    def test_provider_and_model_overrides_win_over_the_file(self):
        plan = plan_hosted_execution(
            configuration(ACTIVATED),
            environ={
                "REVIEWSENSEI_PROVIDER": "openrouter",
                "REVIEWSENSEI_MODEL": "deepseek/deepseek-v4.1-flash",
            },
        )
        self.assertEqual(plan.backend, "openrouter")
        self.assertEqual(plan.model, "deepseek/deepseek-v4.1-flash")
        self.assertEqual(plan.credential_env, "OPENROUTER_API_KEY")
        self.assertEqual(plan.upstream_provider, "morph")

    def test_explicit_invocation_wins_over_the_environment(self):
        plan = plan_hosted_execution(
            configuration(ACTIVATED),
            cli_provider="cloud-ollama",
            cli_model="deepseek-v4.1-pro:cloud",
            environ={
                "REVIEWSENSEI_PROVIDER": "openrouter",
                "REVIEWSENSEI_MODEL": "deepseek/deepseek-v4.1-flash",
            },
        )
        self.assertEqual(plan.backend, "cloud-ollama")
        self.assertEqual(plan.model, "deepseek-v4.1-pro:cloud")
        self.assertEqual(plan.source_of("backend").source, "command line")
        self.assertEqual(plan.source_of("model").source, "command line")

    def test_whitespace_only_overrides_are_not_overrides(self):
        plan = plan_hosted_execution(
            configuration(ACTIVATED),
            environ={
                "REVIEWSENSEI_PROVIDER": "   ",
                "REVIEWSENSEI_MODEL": "\t",
            },
        )
        self.assertEqual(plan.backend, "cloud-ollama")
        self.assertEqual(plan.model, "deepseek-v4.1-flash:cloud")
        self.assertEqual(plan.source_of("backend").source, "configuration file")

    def test_ollama_model_is_never_reinterpreted_as_an_openrouter_slug(self):
        with self.assertRaises(ConfigurationError) as caught:
            plan_hosted_execution(
                configuration(LOCAL),
                environ={"REVIEWSENSEI_PROVIDER": "openrouter"},
            )
        message = str(caught.exception)
        self.assertIn("inference.model", message)
        self.assertIn("not valid for backend 'openrouter'", message)
        self.assertIn(".reviewsensei.yml", message)

    def test_openrouter_slug_is_never_reinterpreted_as_an_ollama_model(self):
        document = (
            "schema: 1\n\ninference:\n  backend: openrouter\n"
            "  model: deepseek/deepseek-v4.1-flash\n"
        )
        with self.assertRaises(ConfigurationError) as caught:
            plan_hosted_execution(
                configuration(document),
                environ={"REVIEWSENSEI_PROVIDER": "cloud-ollama"},
            )
        message = str(caught.exception)
        self.assertIn("inference.model", message)
        self.assertIn("cloud-ollama", message)

    def test_retired_provider_chains_are_rejected_with_a_remedy(self):
        remedies = {
            "REVIEWSENSEI_PROVIDER_MODE": "inference.backend",
            "REVIEWSENSEI_LOCAL_MODEL": "inference.model",
            "REVIEWSENSEI_CLOUD_MODEL": "inference.model",
        }
        for name, replacement in remedies.items():
            with self.subTest(setting=name):
                with self.assertRaises(ConfigurationError) as caught:
                    plan_hosted_execution(
                        configuration(LOCAL), environ={name: "cloud"}
                    )
                message = str(caught.exception)
                self.assertIn(name, message)
                self.assertIn("retired", message)
                self.assertIn(replacement, message)

    def test_fixture_backend_is_not_selectable_for_hosted_execution(self):
        document = "schema: 1\n\ninference:\n  backend: fixture\n"
        with self.assertRaises(ConfigurationError) as caught:
            plan_hosted_execution(configuration(document), environ={})
        self.assertIn("fixture", str(caught.exception))

    def test_every_packaged_backend_reports_a_supported_runner_kind(self):
        for name, defaults in BACKEND_DEFAULTS.items():
            with self.subTest(backend=name):
                self.assertIn(defaults.runner_kind, RUNNER_KINDS)

    def test_custom_credential_reference_is_resolved_for_openai_compatible(self):
        document = (
            "schema: 1\n\ninference:\n  backend: openai-compatible\n"
            "  model: gpt-4o-mini\n\nadvanced:\n  endpoint:\n"
            "    base_url: https://gateway.example.com/v1\n"
            "    allow_custom_endpoint: true\n"
            "    credential_env: GATEWAY_API_KEY\n"
        )
        plan = plan_hosted_execution(
            configuration(document), environ={"GATEWAY_API_KEY": "present"}
        )
        self.assertEqual(plan.credential_env, "GATEWAY_API_KEY")
        self.assertTrue(plan.credential_present)
        self.assertEqual(plan.runner_kind, "hosted")

    def test_a_credential_value_is_never_part_of_the_plan(self):
        plan = plan_hosted_execution(
            configuration(ACTIVATED),
            environ={"OLLAMA_API_KEY": "hosted-secret-value"},
        )
        self.assertTrue(plan.credential_present)
        rendered = render_hosted_plan(plan, as_json=True)
        self.assertIn("OLLAMA_API_KEY", rendered)
        self.assertNotIn("hosted-secret-value", rendered)
        self.assertNotIn("hosted-secret-value", "".join(
            value for _, value in hosted_plan_outputs(plan)
        ))


class HostedPlanOutputTests(unittest.TestCase):
    def test_outputs_export_only_non_secret_decisions(self):
        plan = plan_hosted_execution(
            configuration(ACTIVATED),
            environ={"OLLAMA_API_KEY": "hosted-secret-value"},
        )
        outputs = dict(hosted_plan_outputs(plan))
        self.assertEqual(outputs["backend"], "cloud-ollama")
        self.assertEqual(outputs["model"], "deepseek-v4.1-flash:cloud")
        self.assertEqual(outputs["base_url"], "https://ollama.com/api")
        self.assertEqual(outputs["runner_kind"], "hosted")
        self.assertEqual(outputs["credential_env"], "OLLAMA_API_KEY")
        self.assertEqual(outputs["credential_required"], "true")
        self.assertEqual(outputs["automatic_reviews"], "true")
        self.assertEqual(outputs["writes"], "true")
        self.assertEqual(outputs["reviews"], "auto-approve")
        self.assertEqual(outputs["mentions"], "true")
        self.assertEqual(outputs["learning"], "disabled")
        self.assertEqual(outputs["artifacts"], "none")
        self.assertNotIn("hosted-secret-value", render_github_outputs(
            hosted_plan_outputs(plan)
        ))

    def test_rendered_outputs_use_a_heredoc_delimiter_per_value(self):
        rendered = render_github_outputs((("model", "qwen3.5:4b"),))
        self.assertEqual(rendered, "model<<RS_MODEL\nqwen3.5:4b\nRS_MODEL\n")

    def test_delimiter_collisions_extend_the_delimiter(self):
        rendered = render_github_outputs((("source", "RS_SOURCE"),))
        self.assertEqual(
            rendered, "source<<RS_SOURCE_EOF\nRS_SOURCE\nRS_SOURCE_EOF\n"
        )

    def test_unsupported_output_names_are_rejected(self):
        with self.assertRaises(ConfigurationError):
            render_github_outputs((("Model", "qwen3.5:4b"),))
        with self.assertRaises(ConfigurationError):
            render_github_outputs((("model", "bad\rvalue"),))

    def test_write_github_outputs_writes_one_explicit_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "outputs.txt"
            write_github_outputs(path, (("runner_kind", "hosted"),))
            self.assertEqual(
                path.read_text(encoding="utf-8"),
                "runner_kind<<RS_RUNNER_KIND\nhosted\nRS_RUNNER_KIND\n",
            )

    def test_write_github_outputs_appends_to_an_environment_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "outputs.txt"
            path.write_text(
                "earlier<<RS_EARLIER\nvalue\nRS_EARLIER\n", encoding="utf-8"
            )
            write_github_outputs(path, (("runner_kind", "hosted"),))
            self.assertEqual(
                path.read_text(encoding="utf-8"),
                "earlier<<RS_EARLIER\nvalue\nRS_EARLIER\n"
                "runner_kind<<RS_RUNNER_KIND\nhosted\nRS_RUNNER_KIND\n",
            )


class HostedPlanCliTests(unittest.TestCase):
    def _write(self, directory, text):
        path = Path(directory) / ".reviewsensei.yml"
        path.write_text(text, encoding="utf-8")
        return path

    def _run(self, arguments, environ):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch.dict("os.environ", environ, clear=True):
            with redirect_stderr(stderr):
                with redirect_stdout(stdout):
                    status = main(arguments)
        return status, stdout.getvalue(), stderr.getvalue()

    def test_host_plan_prints_the_resolved_plan_and_exits_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, ACTIVATED)
            status, stdout, stderr = self._run(
                ["host-plan", "--config", str(path)], environ={}
            )
        self.assertEqual(status, 0, stderr)
        self.assertIn("backend: cloud-ollama", stdout)
        self.assertIn("runner requirement: hosted", stdout)
        self.assertIn("credential: OLLAMA_API_KEY, required, absent", stdout)
        self.assertIn("reviews=auto-approve", stdout)

    def test_host_plan_json_includes_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, ACTIVATED)
            status, stdout, _ = self._run(
                ["host-plan", "--config", str(path), "--json"],
                environ={"REVIEWSENSEI_MODEL": "deepseek-v4.1-pro:cloud"},
            )
        self.assertEqual(status, 0)
        document = json.loads(stdout)
        self.assertEqual(document["model"], "deepseek-v4.1-pro:cloud")
        self.assertEqual(
            document["provenance"]["model"]["source"], "environment variable"
        )

    def test_host_plan_writes_the_workflow_output_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, ACTIVATED)
            output = Path(tmp) / "github-output.txt"
            status, _, stderr = self._run(
                [
                    "host-plan",
                    "--config",
                    str(path),
                    "--github-output",
                    str(output),
                ],
                environ={},
            )
            rendered = output.read_text(encoding="utf-8")
        self.assertEqual(status, 0, stderr)
        self.assertIn("backend<<RS_BACKEND\ncloud-ollama\nRS_BACKEND\n", rendered)
        self.assertIn("runner_kind<<RS_RUNNER_KIND\nhosted\nRS_RUNNER_KIND\n", rendered)

    def test_host_plan_reports_invalid_combinations_and_exits_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, LOCAL)
            status, _, stderr = self._run(
                ["host-plan", "--config", str(path)],
                environ={"REVIEWSENSEI_PROVIDER": "openrouter"},
            )
        self.assertEqual(status, 2)
        self.assertIn("inference.model", stderr)

    def test_host_plan_reports_a_missing_configuration_file(self):
        status, _, stderr = self._run(
            ["host-plan", "--config", "/nonexistent/.reviewsensei.yml"],
            environ={},
        )
        self.assertEqual(status, 2)
        self.assertIn("configuration file not found", stderr)

    def test_host_plan_defaults_to_packaged_defaults_without_a_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("review_sensei.configuration.Path.cwd", return_value=Path(tmp)):
                plan = load_and_plan_hosted(None, environ={})
        self.assertEqual(plan.source, None)
        self.assertEqual(plan.backend, "local-ollama")


if __name__ == "__main__":
    unittest.main()
