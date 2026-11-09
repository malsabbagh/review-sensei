"""Public configuration and CLI activation of the opt-in review decision pass."""

from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from isolated_working_directory import IsolatedWorkingDirectoryMixin

from review_sensei.cli import main
from review_sensei.configuration import default_configuration, parse_configuration_text
from review_sensei.decision_configuration import DecisionConfiguration
from review_sensei.errors import ReviewInputError
from review_sensei.hosted import plan_hosted_execution, render_hosted_plan
from review_sensei.providers.decision import DecisionRegistry, default_decision_registry
from review_sensei.review_decision_contract import (
    ReviewDecisionAnswer,
    ReviewDecisionResponse,
)

DIFF = """diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1 +1,2 @@
 keep
+change
"""
LOCAL = {"enabled": True, "provider": "ollama", "model": "decision-model"}
REMOTE = {
    "enabled": True,
    "provider": "openrouter",
    "model": "decision/model",
    "base_url": "https://openrouter.ai/api/v1",
    "api_key_env": "DECISION_TEST_KEY",
    "allow_remote": True,
    "upstream_provider": "morph",
}


def configuration_text(decision=None):
    text = "schema: 1\ninference:\n  backend: local-ollama\n  model: review-model\n"
    if decision is not None:
        text += "decision:\n" + "".join(
            f"  {key}: {json.dumps(value)}\n" for key, value in decision.items()
        )
    return text


def parse(decision=None):
    return parse_configuration_text(configuration_text(decision))


class ReviewDecisionConfigurationTests(unittest.TestCase):
    def test_existing_configuration_keeps_decisions_disabled(self):
        for configuration in (
            default_configuration(),
            parse(),
            parse({"enabled": False}),
        ):
            with self.subTest(source=configuration.source):
                self.assertEqual(configuration.decision, DecisionConfiguration())
                self.assertIsNone(
                    default_decision_registry().create(configuration.decision, {})
                )

    def test_canonical_yaml_loads_separate_local_model_endpoint_and_bounds(self):
        values = {
            **LOCAL,
            "base_url": "http://127.0.0.1:11435/api",
            "max_calls": 2,
            "max_input_bytes": 16384,
            "max_output_bytes": 8192,
            "timeout_ms": 5000,
        }
        configuration = parse(values)
        self.assertEqual(configuration.inference.model, "review-model")
        self.assertEqual(configuration.decision.model, "decision-model")
        self.assertEqual(configuration.decision.endpoint, values["base_url"])
        self.assertIsNone(configuration.decision.api_key_env)
        self.assertFalse(configuration.decision.allow_remote)
        for name in ("max_calls", "max_input_bytes", "max_output_bytes", "timeout_ms"):
            self.assertEqual(getattr(configuration.decision, name), values[name])
        self.assertTrue(configuration.is_declared("decision.model"))

    def test_remote_configuration_requires_independent_explicit_route(self):
        configuration = parse(REMOTE)
        self.assertEqual(configuration.decision.model, "decision/model")
        self.assertEqual(configuration.decision.api_key_env, "DECISION_TEST_KEY")
        self.assertEqual(configuration.decision.upstream_provider, "morph")
        registry = default_decision_registry()
        with self.assertRaises(ReviewInputError):
            registry.create(
                configuration.decision,
                {"OPENROUTER_API_KEY": "synthetic-review-key"},
            )
        adapter = registry.create(
            configuration.decision,
            {
                "OPENROUTER_API_KEY": "synthetic-review-key",
                "DECISION_TEST_KEY": "synthetic-decision-key",
            },
        )
        self.assertEqual(adapter.key, "synthetic-decision-key")

    def test_remote_cannot_inherit_model_credential_or_privacy_permission(self):
        for omitted in ("model", "api_key_env", "allow_remote", "upstream_provider"):
            with self.subTest(omitted=omitted), self.assertRaises(ReviewInputError):
                parse({key: value for key, value in REMOTE.items() if key != omitted})

    def test_local_only_configuration_refuses_cloud_models_and_nonloopback_routes(self):
        for changes in (
            {"base_url": "https://ollama.com/api"},
            {"base_url": "http://localhost:11434/api"},
            {"base_url": "http://192.0.2.1:11434/api"},
            {"model": "review-model:cloud"},
            {"api_key_env": "OPENROUTER_API_KEY"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ReviewInputError):
                parse({**LOCAL, **changes})
        with self.assertRaises(ReviewInputError):
            parse({**REMOTE, "allow_remote": False})

    def test_unknown_fields_and_unsupported_provider_do_not_silently_disable(self):
        for changes in (
            {"provider": "unsupported"},
            {"fallback_model": "other-model"},
            {"api_key": "synthetic-inline-secret"},
            {"data_collection": "allow"},
            {"required_privacy_control": "unsupported"},
            {"enabled": "true"},
            {"max_calls": True},
            {"max_calls": 0},
            {"timeout_ms": 120001},
        ):
            with self.subTest(changes=changes), self.assertRaises(ReviewInputError):
                parse({**LOCAL, **changes})

    def test_hosted_plan_displays_explicit_local_decision_and_disabled_default(self):
        for settings in (None, LOCAL):
            with self.subTest(settings=settings):
                configuration = parse(settings)
                plan = plan_hosted_execution(configuration, environ={})
                document = json.loads(render_hosted_plan(plan, as_json=True))
                self.assertEqual(document["model"], "review-model")
                self.assertEqual(document["decision"], configuration.decision.to_dict())
                self.assertEqual(document["decision"]["enabled"], settings is not None)

    def test_hosted_plan_refuses_decision_route_unavailable_on_selected_runner(self):
        # A local review runner does not silently inherit credentials for remote triage.
        with self.assertRaises(ReviewInputError):
            plan_hosted_execution(
                parse(REMOTE), environ={"DECISION_TEST_KEY": "synthetic-decision-key"}
            )
        configuration = parse_configuration_text(
            configuration_text(LOCAL).replace("local-ollama", "cloud-ollama")
        )
        with self.assertRaises(ReviewInputError):
            plan_hosted_execution(configuration, environ={})


class FakeDecisionProvider:
    name = "ollama"
    model = "decision-model"

    def __init__(self):
        self.requests = []

    def evaluate(self, request, execution):
        self.requests.append(request)
        execution.execute(request.state, lambda timeout, maximum: "{}")
        choices = {
            "kind": "behavioral_defect",
            "evidence": "supported",
            "next": "continue",
            "duplicate": "different_concern",
        }
        return ReviewDecisionResponse(
            request.digest(),
            self.name,
            self.model,
            self.model,
            tuple(
                ReviewDecisionAnswer(
                    question.name, choices[question.name.rsplit("_", 1)[1]]
                )
                for question in request.questions
            ),
        )


class ReviewDecisionCliTests(IsolatedWorkingDirectoryMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.diff = Path("review.patch")
        self.diff.write_bytes(DIFF.encode("utf-8"))
        self.config = Path(".reviewsensei.yml")
        self.fixture = Path("response.json")
        self.fixture.write_text(
            json.dumps(
                {
                    "summary": "Synthetic regression found.",
                    "comments": [
                        {
                            "path": "src/app.py",
                            "line": 2,
                            "body": "The changed behavior requires a regression fix.",
                            "severity": "high",
                            "category": "correctness",
                        }
                    ],
                    "learning_proposals": [],
                }
            ),
            encoding="utf-8",
        )

    def invoke(self, arguments, *, environment=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch.dict(os.environ, environment or {}, clear=True),
            patch(
                "socket.socket.connect", side_effect=AssertionError("network forbidden")
            ),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            status = main(arguments)
        return status, stdout.getvalue(), stderr.getvalue()

    def review(self):
        return self.invoke(
            [
                "--config",
                str(self.config),
                "--provider",
                "fixture",
                "--fixture-response",
                str(self.fixture),
                "--diff",
                str(self.diff),
                "--no-learning-proposals",
                "--format",
                "json",
            ]
        )

    def test_real_cli_route_dispatches_decision_adapter_and_preserves_blocker(self):
        adapter = FakeDecisionProvider()
        registry = DecisionRegistry()
        registry.register("ollama", lambda settings, environment: adapter)
        self.config.write_text(configuration_text(LOCAL), encoding="utf-8")
        with patch(
            "review_sensei.cli.default_decision_registry", return_value=registry
        ):
            status, stdout, stderr = self.review()
        self.assertEqual(status, 1, stderr)
        document = json.loads(stdout)
        self.assertEqual(len(adapter.requests), 1)
        receipt = document["decision_pass"]
        self.assertEqual(receipt["status"], "complete")
        self.assertEqual(receipt["provider"], "ollama")
        self.assertEqual(receipt["model"], "decision-model")
        self.assertEqual(receipt["calls"], 1)
        self.assertEqual(receipt["candidates"][0]["action"], "continue")
        self.assertEqual(document["comments"][0]["severity"], "high")
        self.assertEqual(document["comments"][0]["path"], "src/app.py")

    def test_disabled_cli_never_constructs_or_dispatches_decision_adapter(self):
        registry = DecisionRegistry()
        factory = unittest.mock.Mock(side_effect=AssertionError("disabled adapter"))
        registry.register("ollama", factory)
        self.config.write_text(configuration_text(), encoding="utf-8")
        with patch(
            "review_sensei.cli.default_decision_registry", return_value=registry
        ):
            status, stdout, stderr = self.review()
        self.assertEqual(status, 1, stderr)
        factory.assert_not_called()
        self.assertNotIn("decision_pass", json.loads(stdout))

    def test_missing_decision_credential_refuses_cli_before_review_dispatch(self):
        self.config.write_text(configuration_text(REMOTE), encoding="utf-8")
        with patch(
            "review_sensei.providers.fixture.FixtureProvider.complete",
            side_effect=AssertionError(
                "review must not start with invalid decision configuration"
            ),
        ) as complete:
            status, stdout, stderr = self.review()
        self.assertEqual(status, 2, stderr)
        self.assertEqual(stdout, "")
        self.assertIn("credential", stderr)
        complete.assert_not_called()

    def test_plan_and_doctor_report_decision_configuration_without_secrets_or_dispatch(
        self,
    ):
        self.config.write_text(configuration_text(REMOTE), encoding="utf-8")
        for command in ("plan", "doctor"):
            with self.subTest(command=command):
                arguments = [command, "--config", str(self.config), "--json"]
                if command == "plan":
                    arguments += ["--diff", str(self.diff)]
                with patch(
                    "review_sensei.cli.default_decision_registry",
                    side_effect=AssertionError("display must not construct an adapter"),
                ):
                    status, stdout, stderr = self.invoke(
                        arguments,
                        environment={"DECISION_TEST_KEY": "synthetic-display-secret"},
                    )
                document = json.loads(stdout)
                if command == "doctor":
                    # This offline fixture has no automated-review session ledger.
                    self.assertEqual(status, 2, stderr)
                    self.assertIn(
                        "automation-admission",
                        [
                            check["name"]
                            for check in document["checks"]
                            if check["status"] == "action"
                        ],
                    )
                else:
                    self.assertEqual(status, 0, stderr)
                    self.assertEqual(document["operations"]["provider_calls"], 0)
                decision = document["provider_configuration"]["decision"]
                self.assertTrue(decision["enabled"])
                self.assertEqual(decision["model"], "decision/model")
                self.assertEqual(decision["api_key_env"], "DECISION_TEST_KEY")
                self.assertTrue(decision["allow_remote"])
                self.assertNotIn("synthetic-display-secret", stdout + stderr)
