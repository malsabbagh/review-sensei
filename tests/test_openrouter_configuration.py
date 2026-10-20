"""Canonical routing configuration through the real CLI and adapter.

Endpoint suffixes are synthetic protocol inputs, not live capability evidence.
"""

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from review_sensei_github.cli import main as github_main

from review_sensei.cli import main
from review_sensei.configuration import load_configuration, resolve_inference
from review_sensei.errors import ReviewInputError
from review_sensei.openrouter_qualification import OpenRouterQualificationTarget
from review_sensei.providers.openrouter import (
    OpenRouterProvider,
    OpenRouterRoutingPolicy,
)
from tests.isolated_working_directory import IsolatedWorkingDirectoryMixin

PATCH = (
    "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-x = 0\n+x = 1\n"
)
KEY = "synthetic-openrouter-routing-key"


class OpenRouterConfigurationTests(IsolatedWorkingDirectoryMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        Path("change.patch").write_text(PATCH, encoding="utf-8")
        self.requests = []
        self.adapters = []

    def write_config(self, slug, model="deepseek/deepseek-v4.1-flash"):
        Path(".reviewsensei.yml").write_text(
            "schema: 1\ninference:\n  backend: openrouter\n"
            f"  model: {model}\nadvanced:\n  routing:\n"
            f"    upstream_provider: {json.dumps(slug)}\n"
            "  egress:\n    allow_data_egress: true\n",
            encoding="utf-8",
        )

    def invoke(self, args, *, legacy_upstream=None):
        def opener(request, timeout, context):
            self.requests.append(json.loads(request.data))
            return io.BytesIO(
                json.dumps(
                    {
                        "choices": [
                            {
                                "message": {
                                    "content": '{"summary":"Synthetic clean","comments":[]}'
                                }
                            }
                        ]
                    }
                ).encode()
            )

        def adapter(**kwargs):
            provider = OpenRouterProvider(_test_opener=opener, **kwargs)
            self.adapters.append(provider)
            return provider

        environment = {"OPENROUTER_API_KEY": KEY}
        if legacy_upstream is not None:
            environment["OPENROUTER_UPSTREAM_PROVIDER"] = legacy_upstream
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch.dict("os.environ", environment, clear=True),
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
            entry = github_main if args[0] == "host-plan" else main
            status = entry(args)
        self.assertNotIn(KEY, stdout.getvalue() + stderr.getvalue())
        return status, stdout.getvalue(), stderr.getvalue()

    def test_yaml_resolver_host_plan_and_cli_preserve_known_family_endpoint(self):
        for model, slug in (
            ("deepseek/deepseek-v4.1-flash", "morph/variant"),
            ("deepseek/deepseek-v4.1-flash", "morph/variant/us/priority"),
            ("anthropic/claude-3.5-sonnet", "anthropic/variant"),
            ("openai/gpt-4o-mini", "openai/variant"),
        ):
            with self.subTest(slug=slug):
                self.write_config(slug, model)
                resolved = resolve_inference(load_configuration(), environ={})
                self.assertEqual(resolved.upstream_provider, slug)
                code, output, error = self.invoke(["config", "validate"])
                self.assertEqual(code, 0, error)
                code, output, error = self.invoke(
                    ["host-plan", "--json", "--github-output", "plan.outputs"]
                )
                self.assertEqual(code, 0, error)
                self.assertEqual(json.loads(output)["upstream_provider"], slug)
                self.assertIn(slug, Path("plan.outputs").read_text())
                code, output, error = self.invoke(["plan", "--json"])
                self.assertEqual(
                    code, 3, error
                )  # no input diff supplied to offline plan
                self.assertEqual(
                    json.loads(output)["provider_configuration"]["openrouter_policy"][
                        "upstream_provider"
                    ],
                    slug,
                )
                before = len(self.requests)
                code, output, error = self.invoke(
                    [
                        "--diff",
                        "change.patch",
                        "--no-learning-proposals",
                        "--format",
                        "json",
                    ]
                )
                self.assertEqual(code, 0, error)
                self.assertEqual(len(self.requests), before + 1)
                self.assertEqual(self.requests[-1]["model"], model)
                self.assertEqual(
                    self.requests[-1]["provider"],
                    OpenRouterRoutingPolicy(slug).to_request_provider(),
                )
                self.assertEqual(
                    self.adapters[-1].routing_policy.upstream_provider, slug
                )
                with self.assertRaises(ReviewInputError):
                    OpenRouterQualificationTarget(model, slug)

    def test_review_keeps_yaml_endpoint_despite_retired_upstream_environment(self):
        self.write_config("morph/variant")
        code, _, error = self.invoke(
            ["--diff", "change.patch", "--no-learning-proposals"],
            legacy_upstream="invalid retired value",
        )
        self.assertEqual(code, 0, error)
        self.assertEqual(self.requests[0]["provider"]["order"], ["morph/variant"])

    def test_canonical_cli_rejects_malformed_and_wrong_family_before_transport(self):
        for slug in (
            "morph//variant",
            "morph/../variant",
            "morph/variant?x",
            "morph/variant#x",
            "morph/\nvariant",
            "morph/" + "v" * 128,
            "anthropic/variant",
            "morph-other/variant",
        ):
            with self.subTest(slug=slug):
                self.write_config(slug)
                for command in (
                    ["config", "validate"],
                    ["host-plan", "--json"],
                    ["--diff", "change.patch", "--no-learning-proposals"],
                ):
                    code, output, error = self.invoke(command)
                    self.assertEqual(code, 2, error)
                    self.assertIn("advanced.routing.upstream_provider", error)
        self.assertEqual(self.adapters, [])
        self.assertEqual(self.requests, [])
