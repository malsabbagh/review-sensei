import io
import re
import shlex
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from review_sensei.cli import main
from review_sensei.errors import ProviderError
from review_sensei.providers.registry import ProviderRegistry

try:
    from isolated_working_directory import IsolatedWorkingDirectoryMixin
except ModuleNotFoundError:
    from tests.isolated_working_directory import IsolatedWorkingDirectoryMixin


class ReplyProvider:
    name = "synthetic"
    model = "synthetic-model"

    def complete(self, request):
        raise AssertionError("reply wiring tests must not call a live provider")


class ReplyInferenceTests(IsolatedWorkingDirectoryMixin, unittest.TestCase):
    def invoke(
        self, extra=(), *, environment=None, factory_error=None, reply_error=None
    ):
        from review_sensei.hosting import github as github_module

        settings = []
        calls = []

        def factory(value):
            settings.append(value)
            if factory_error:
                raise factory_error
            return ReplyProvider()

        registry = ProviderRegistry()
        for adapter in ("ollama", "openai-compatible", "openrouter"):
            registry.register(adapter, factory)

        class Application:
            def __init__(self, **kwargs):
                pass

            def generate_and_publish_reply(self, **kwargs):
                calls.append(kwargs)
                if reply_error:
                    raise reply_error
                return SimpleNamespace(status="replied")

        argv = [
            "github",
            "reply",
            "--generate",
            "--repository",
            "owner/repo",
            "--pull-request",
            "2",
            "--source-comment-id",
            "10",
            "--source-updated-at",
            "2026-10-05T00:00:00Z",
            "--source-kind",
            "issue",
            "--allow-write",
            "--enable-reply",
            *extra,
        ]
        stdout, stderr = io.StringIO(), io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(
                patch.dict(
                    "os.environ",
                    {"GITHUB_TOKEN": "synthetic-read-token", **(environment or {})},
                    clear=True,
                )
            )
            stack.enter_context(
                patch.multiple(
                    github_module,
                    BrokerClient=lambda: object(),
                    GitHubHttp=lambda: object(),
                    ReviewPublisher=lambda **kwargs: object(),
                    LearningPRPublisher=lambda **kwargs: object(),
                    ConversationPublisher=lambda **kwargs: object(),
                    GitHubApplication=Application,
                )
            )
            stack.enter_context(
                patch("review_sensei.cli.default_registry", return_value=registry)
            )
            stack.enter_context(redirect_stdout(stdout))
            stack.enter_context(redirect_stderr(stderr))
            status = main(argv)
        return status, settings, calls, stdout.getvalue(), stderr.getvalue()

    def test_workflow_backend_names_reach_registered_ollama_adapter(self):
        workflow = (
            Path(__file__).resolve().parents[1]
            / ".github/workflows/review-sensei-run.yml"
        ).read_text(encoding="utf-8")
        argument_blocks = re.findall(r"reply_args=\((.*?)\)\n", workflow, re.DOTALL)
        self.assertEqual(
            len(argument_blocks), 2, "exercise hosted and local reply argv"
        )
        for backend, model, endpoint, credential in (
            (
                "cloud-ollama",
                "deepseek-v4.1-flash:cloud",
                "https://ollama.com/api",
                "synthetic-cloud-key",
            ),
            ("local-ollama", "qwen3.5:4b", "http://127.0.0.1:11434/api", None),
        ):
            for block in argument_blocks:
                with self.subTest(backend=backend, block=block):
                    values = {
                        "BACKEND": backend,
                        "MODEL": model,
                        "BASE_URL": endpoint,
                        "REPOSITORY": "owner/repo",
                        "PULL_REQUEST": "2",
                        "SOURCE_COMMENT_ID": "10",
                        "SOURCE_UPDATED_AT": "2026-10-05T00:00:00Z",
                        "HEAD_SHA": "synthetic-head",
                        "ROOT_COMMENT_ID": "10",
                        "SOURCE_KIND": "issue",
                    }
                    # Parse the checked-in array as data; never execute workflow shell.
                    expanded = re.sub(
                        r"\$([A-Z_]+)", lambda match: values[match.group(1)], block
                    )
                    extra = shlex.split(expanded.replace("\\\n", ""))
                    if credential:
                        extra += ["--api-key-env", "OLLAMA_API_KEY"]
                    status, settings, calls, stdout, stderr = self.invoke(
                        extra, environment={"OLLAMA_API_KEY": "synthetic-cloud-key"}
                    )
                    self.assertEqual(status, 0, stderr)
                    self.assertEqual(stdout.strip(), "replied")
                    self.assertEqual(len(calls), 1)
                    self.assertEqual(settings[0].name, "ollama")
                    self.assertEqual(settings[0].base_url, endpoint)
                    self.assertEqual(settings[0].model, model)
                    self.assertEqual(settings[0].api_key, credential)
                    self.assertEqual(calls[0]["model"], model)

    def test_explicit_adapter_endpoint_key_and_timeout_are_preserved(self):
        for adapter in ("openai-compatible", "openrouter", "local-ollama"):
            with self.subTest(adapter=adapter):
                model = (
                    "vendor/synthetic-model"
                    if adapter == "openrouter"
                    else "synthetic-model"
                )
                endpoint = {
                    "openai-compatible": "https://api.openai.com/v1",
                    "openrouter": "https://openrouter.ai/api/v1",
                    "local-ollama": "http://127.0.0.1:11434/api",
                }[adapter]
                status, settings, _, _, stderr = self.invoke(
                    [
                        f"--provider={adapter}",
                        f"--model={model}",
                        f"--base-url={endpoint}",
                        "--api-key-env=SYNTHETIC_PROVIDER_KEY",
                        "--timeout-seconds=17",
                    ],
                    environment={
                        "SYNTHETIC_PROVIDER_KEY": "synthetic-explicit-key",
                        "OLLAMA_API_KEY": "unrelated-synthetic-key",
                    },
                )
                self.assertEqual(status, 0, stderr)
                self.assertEqual(
                    settings[0].name, "ollama" if adapter == "local-ollama" else adapter
                )
                self.assertEqual(settings[0].base_url, endpoint)
                self.assertEqual(settings[0].api_key, "synthetic-explicit-key")
                self.assertEqual(settings[0].timeout_seconds, 17)

    def test_supported_environment_backend_override_uses_canonical_resolution(self):
        status, settings, _, _, stderr = self.invoke(
            environment={
                "REVIEWSENSEI_PROVIDER": "cloud-ollama",
                "REVIEWSENSEI_MODEL": "synthetic-cloud-model",
                "OLLAMA_API_KEY": "synthetic-cloud-key",
            }
        )
        self.assertEqual(status, 0, stderr)
        self.assertEqual(settings[0].base_url, "https://ollama.com/api")
        self.assertEqual(settings[0].model, "synthetic-cloud-model")

    def test_reply_fixture_backend_fails_with_input_error_before_publication(self):
        status, settings, calls, _, stderr = self.invoke(["--provider", "fixture"])
        self.assertEqual(status, 1)
        self.assertEqual(settings, [])
        self.assertEqual(calls, [])
        self.assertIn("requires --fixture-response", stderr)

    def test_direct_cloud_reply_uses_cloud_endpoint_model_and_credential(self):
        status, settings, calls, _, stderr = self.invoke(
            ["--provider", "cloud-ollama"],
            environment={"OLLAMA_API_KEY": "synthetic-cloud-key"},
        )
        self.assertEqual(status, 0, stderr)
        self.assertEqual(settings[0].name, "ollama")
        self.assertEqual(settings[0].base_url, "https://ollama.com/api")
        self.assertEqual(settings[0].model, "deepseek-v4.1-flash:cloud")
        self.assertEqual(settings[0].api_key, "synthetic-cloud-key")
        self.assertEqual(calls[0]["model"], settings[0].model)

    def test_default_local_reply_does_not_inherit_cloud_credential(self):
        status, settings, _, _, stderr = self.invoke(
            environment={"OLLAMA_API_KEY": "synthetic-cloud-key"}
        )
        self.assertEqual(status, 0, stderr)
        self.assertEqual(settings[0].name, "ollama")
        self.assertEqual(settings[0].base_url, "http://127.0.0.1:11434/api")
        self.assertIsNone(settings[0].api_key)

    def test_reply_config_and_cli_precedence_preserve_local_credential_boundary(self):
        config = Path("reply.yml")
        config.write_text(
            "schema: 1\ninference:\n  backend: cloud-ollama\n  model: deepseek-v4.1-flash:cloud\n",
            encoding="utf-8",
        )
        status, settings, _, _, stderr = self.invoke(
            ["--config", str(config)],
            environment={"OLLAMA_API_KEY": "synthetic-cloud-key"},
        )
        self.assertEqual(status, 0, stderr)
        self.assertEqual(settings[0].base_url, "https://ollama.com/api")
        status, settings, _, _, stderr = self.invoke(
            [
                "--config",
                str(config),
                "--provider",
                "local-ollama",
                "--model",
                "qwen3.5:4b",
            ],
            environment={"OLLAMA_API_KEY": "synthetic-cloud-key"},
        )
        self.assertEqual(status, 0, stderr)
        self.assertEqual(settings[0].base_url, "http://127.0.0.1:11434/api")
        self.assertIsNone(settings[0].api_key)

    def test_missing_cloud_credential_fails_before_provider_or_publication(self):
        status, settings, calls, _, stderr = self.invoke(["--provider", "cloud-ollama"])
        self.assertEqual(status, 1)
        self.assertEqual(settings, [])
        self.assertEqual(calls, [])
        self.assertIn("OLLAMA_API_KEY is unavailable", stderr)

    def test_unknown_backend_and_invalid_config_fail_before_publication(self):
        config = Path("invalid.yml")
        config.write_text("schema: 99\n", encoding="utf-8")
        for extra in (["--provider", "unknown-backend"], ["--config", str(config)]):
            with self.subTest(extra=extra):
                status, settings, calls, _, _ = self.invoke(extra)
                self.assertEqual(status, 1)
                self.assertEqual(settings, [])
                self.assertEqual(calls, [])

    def test_local_profile_drops_ambient_cloud_key_and_rejects_endpoint_override(self):
        status, settings, _, _, stderr = self.invoke(
            ["--profile", "local-private", "--provider", "local-ollama"],
            environment={"OLLAMA_API_KEY": "synthetic-cloud-key"},
        )
        self.assertEqual(status, 0, stderr)
        self.assertEqual(settings[0].name, "ollama")
        self.assertIsNone(settings[0].api_key)
        status, settings, calls, _, _ = self.invoke(
            ["--profile", "local-private", "--base-url", "https://invalid.example/api"]
        )
        self.assertEqual(status, 1)
        self.assertEqual(settings, [])
        self.assertEqual(calls, [])

    def test_provider_failure_does_not_publish_and_reply_failure_exits_one(self):
        status, _, calls, _, stderr = self.invoke(
            factory_error=ProviderError("synthetic provider failure")
        )
        self.assertEqual(status, 1)
        self.assertEqual(calls, [])
        self.assertIn("synthetic provider failure", stderr)
        status, _, calls, _, stderr = self.invoke(
            reply_error=ProviderError("synthetic reply failure")
        )
        self.assertEqual(status, 1)
        self.assertEqual(len(calls), 1)
        self.assertIn("synthetic reply failure", stderr)


if __name__ == "__main__":
    unittest.main()
