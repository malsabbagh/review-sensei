"""Public CLI and actual OpenRouter adapter, with synthetic HTTPS transport.

Reusable in checkout and installed-wheel lanes. No live endpoint, broker or
credential is contacted; sentinels establish selected credential isolation.
"""

import io
import json
import ssl
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from review_sensei.cli import main
from review_sensei.providers.openrouter import OpenRouterProvider

try:
    from isolated_working_directory import IsolatedWorkingDirectoryMixin
except ModuleNotFoundError:
    from tests.isolated_working_directory import IsolatedWorkingDirectoryMixin

PATCH = (
    "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-x = 0\n+x = 1\n"
)
MODEL = "deepseek/deepseek-v4.1-flash"
SENTINELS = {
    "OPENROUTER_API_KEY": "synthetic-selected-router-key",
    "OLLAMA_API_KEY": "synthetic-unselected-ollama-key",
    "OPENAI_API_KEY": "synthetic-unselected-openai-key",
    "CUSTOM_ROUTER_KEY": "synthetic-selected-custom-key",
}


class OpenRouterCLIContract(IsolatedWorkingDirectoryMixin):
    def setUp(self):
        super().setUp()
        Path("change.patch").write_text(PATCH, encoding="utf-8")
        self.requests = []
        self.environment = dict(SENTINELS)
        self.envelope = {
            "model": MODEL,
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": json.dumps(
                            {"summary": "Synthetic clean", "comments": []}
                        )
                    },
                }
            ],
        }

    def config(self, *, backend="openrouter", endpoint=""):
        Path(".reviewsensei.yml").write_text(
            f"schema: 1\ninference:\n  backend: {backend}\n  model: {MODEL}\n"
            f"advanced:\n{endpoint}  routing:\n    upstream_provider: morph\n",
            encoding="utf-8",
        )

    def invoke(self):
        def opener(request, timeout, context):
            self.requests.append((request, timeout, context))
            response = io.BytesIO(json.dumps(self.envelope).encode())
            response.status = 200
            return response

        def adapter(**kwargs):
            return OpenRouterProvider(_test_opener=opener, **kwargs)

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
            patch("review_sensei.providers.registry.OpenRouterProvider", adapter),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            status = main(
                [
                    "--diff",
                    "change.patch",
                    "--format",
                    "json",
                    "--no-learning-proposals",
                ]
            )
        text = stdout.getvalue() + stderr.getvalue()
        for value in SENTINELS.values():
            self.assertNotIn(value, text)
            for path in Path.cwd().rglob("*"):
                if path.is_file():
                    self.assertNotIn(value.encode(), path.read_bytes())
        return status, stdout.getvalue(), stderr.getvalue()

    def test_selected_credential_and_strict_routing_reach_real_adapter(self):
        self.config()
        status, output, error = self.invoke()
        self.assertEqual(status, 0, error)
        self.assertEqual(json.loads(output)["review_status"], "complete")
        self.assertTrue(self.requests)
        for request, timeout, context in self.requests:
            self.assertEqual(
                request.full_url, "https://openrouter.ai/api/v1/chat/completions"
            )
            self.assertEqual(
                request.get_header("Authorization"),
                "Bearer " + SENTINELS["OPENROUTER_API_KEY"],
            )
            payload = json.loads(request.data)
            self.assertEqual(payload["model"], MODEL)
            policy = payload["provider"]
            self.assertEqual(policy["order"], ["morph"])
            self.assertIs(policy["allow_fallbacks"], False)
            self.assertIs(policy["require_parameters"], True)
            self.assertEqual(policy["data_collection"], "deny")
            self.assertIs(policy["zdr"], True)
            self.assertGreater(timeout, 0)
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(context.check_hostname)

    def test_missing_selected_key_cannot_borrow_another_provider_credential(self):
        self.config()
        self.environment.pop("OPENROUTER_API_KEY")
        status, _, error = self.invoke()
        self.assertEqual(status, 2)
        self.assertIn("OPENROUTER_API_KEY", error)
        self.assertEqual(self.requests, [])

    def test_unsupported_custom_credential_refuses_before_transport(self):
        self.config(endpoint="  endpoint:\n    credential_env: CUSTOM_ROUTER_KEY\n")
        status, _, error = self.invoke()
        self.assertEqual(status, 2)
        self.assertIn("only supported for backend", error)
        self.assertEqual(self.requests, [])

    def test_incompatible_backend_override_refuses_before_transport(self):
        self.config()
        self.environment["REVIEWSENSEI_PROVIDER"] = "cloud-ollama"
        status, _, error = self.invoke()
        self.assertEqual(status, 2)
        self.assertIn("inference.model", error)
        self.assertEqual(self.requests, [])

    def test_malformed_response_remains_incomplete_and_redacts_all_sentinels(self):
        self.config()
        self.envelope = {"choices": [], "private": dict(SENTINELS)}
        status, _, error = self.invoke()
        self.assertEqual(status, 2)
        self.assertTrue(self.requests)
        self.assertNotIn("Traceback", error)
