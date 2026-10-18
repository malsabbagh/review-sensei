"""One engine-flow contract for actual adapters with synthetic HTTP envelopes.

Factories vary registry settings, transport injection and protocol envelopes;
every factory runs the identical methods below. This qualifies composition at
library seams, not live capabilities, hosted delivery or model quality.
"""

from __future__ import annotations

import io
import itertools
import json
import socket
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import dataclass, replace
from pathlib import Path
from unittest.mock import patch

from review_sensei_github.approval import (
    ReviewApprovalEligibility,
    approval_eligibility_from_result,
    approval_facts_from_result,
    evaluate_approval_facts,
)
from review_sensei_github.checks import check_outcome_for_result
from review_sensei_github.operation_comments import ResultPartStore
from review_sensei_github.operation_entry import (
    analysis_payload,
    call_operation_trigger,
)
from review_sensei_github.operation_host import GitHubCommentOperationStore

from review_sensei.baseline import (
    baseline_from_review,
    plan_verification_scope,
    reconcile_overflow_review,
)
from review_sensei.budgets import ProviderCapabilities, ReviewWorkBudgets
from review_sensei.cli import main
from review_sensei.context import ReviewContextCacheKey
from review_sensei.convergence import ReviewConvergencePolicy
from review_sensei.conversation import ConversationService
from review_sensei.errors import ProviderError, ReviewInputError
from review_sensei.human_assessment import HumanAssessmentService
from review_sensei.models import (
    ConversationContext,
    ReviewComment,
    ReviewRequest,
    ReviewResult,
)
from review_sensei.outcomes import ResourceBudget, ResourceBudgetTracker
from review_sensei.providers import registry
from review_sensei.providers.ollama import OllamaProvider
from review_sensei.providers.openrouter import OpenRouterRoutingPolicy
from review_sensei.providers.registry import ProviderSettings
from review_sensei.service import ReviewService
from review_sensei.stages import Stage

DIFF = "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-return send(data)\n+return reject_remote_requests(data)\n"
BASE, HEAD = "a" * 40, "b" * 40
CLEAN = '{"summary":"Synthetic complete review.","comments":[]}'
SOURCE = "This path is intentionally local-only; remote requests are rejected."


class Clock:
    value = 0.0

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


@dataclass(frozen=True)
class AdapterCase:
    name: str
    settings: ProviderSettings
    constructor: str
    cli_backend: str
    cli_inference_yaml: str = ""
    credential_env: str | None = None
    cli_advanced_yaml: str = ""
    cli_negative_overrides: tuple[str, ...] = ()
    transport_argument: str = "opener"
    ollama_envelope: bool = False
    approval_without_qualification: bool = True

    def configuration(self, *, advanced_yaml=None):
        return (
            "schema: 1\ninference:\n  backend: "
            + self.cli_backend
            + "\n"
            + self.cli_inference_yaml
            + "advanced:\n  egress:\n    allow_data_egress: true\n"
            + (self.cli_advanced_yaml if advanced_yaml is None else advanced_yaml)
        )

    def envelope(self, text):
        if self.ollama_envelope:
            return {
                "response": text,
                "model": self.settings.model,
                "done": True,
                "done_reason": "stop",
                "eval_count": 1,
            }
        return {"choices": [{"message": {"content": text}, "finish_reason": "stop"}]}

    def prompt(self, payload):
        return (
            payload["prompt"]
            if self.ollama_envelope
            else payload["messages"][-1]["content"]
        )

    def output_tokens(self, payload):
        return (
            payload["options"]["num_predict"]
            if self.ollama_envelope
            else payload["max_tokens"]
        )

    @contextmanager
    def transport_scope(self, transport):
        # The real default registry chooses all adapter options. Only its
        # constructor's documented transport seam is supplied by this harness.
        constructor = getattr(registry, self.constructor)

        def captured(**kwargs):
            return constructor(**kwargs, **{self.transport_argument: transport})

        with patch.object(registry, self.constructor, captured):
            yield

    def create(self, transport, *, model=None):
        with self.transport_scope(transport):
            return registry.default_registry().create(
                replace(self.settings, model=model)
                if model is not None
                else self.settings
            )


ADAPTER_CASES = (
    AdapterCase(
        "OllamaCloud",
        ProviderSettings(
            name="ollama",
            model="deepseek-v4.1-flash:cloud",
            base_url="https://ollama.com/api",
            api_key="synthetic-only",
            timeout_seconds=5,
            max_output_tokens=128,
        ),
        "OllamaProvider",
        cli_backend="cloud-ollama",
        credential_env="OLLAMA_API_KEY",
        ollama_envelope=True,
    ),
    AdapterCase(
        "OllamaLocalDefault",
        ProviderSettings(
            name="ollama",
            model="qwen3.5:4b",
            base_url="http://127.0.0.1:11434/api",
            timeout_seconds=5,
            max_output_tokens=128,
        ),
        "OllamaProvider",
        cli_backend="local-ollama",
        ollama_envelope=True,
    ),
    AdapterCase(
        "OllamaCloudGLM",
        ProviderSettings(
            name="ollama",
            model="glm-5.3:cloud",
            base_url="https://ollama.com/api",
            api_key="synthetic-only",
            timeout_seconds=5,
            max_output_tokens=128,
        ),
        "OllamaProvider",
        cli_backend="cloud-ollama",
        cli_inference_yaml="  model: glm-5.3:cloud\n",
        credential_env="OLLAMA_API_KEY",
        ollama_envelope=True,
    ),
    AdapterCase(
        "OpenAIHTTPS",
        ProviderSettings(
            name="openai-compatible",
            model="gpt-4o-mini",
            base_url="https://api.openai.com/v1",
            api_key="synthetic-only",
            timeout_seconds=5,
            max_output_tokens=128,
        ),
        "OpenAICompatibleProvider",
        cli_backend="openai-compatible",
        credential_env="OPENAI_API_KEY",
    ),
    AdapterCase(
        "OpenAICustomHTTPS",
        ProviderSettings(
            name="openai-compatible",
            model="gpt-4o-mini",
            base_url="https://gateway.example.test/v1",
            api_key="selected-custom-only",
            timeout_seconds=5,
            max_output_tokens=128,
            allow_custom_endpoint=True,
        ),
        "OpenAICompatibleProvider",
        cli_backend="openai-compatible",
        credential_env="CUSTOM_OPENAI_KEY",
        cli_advanced_yaml="  endpoint:\n    base_url: https://gateway.example.test/v1\n    allow_custom_endpoint: true\n    credential_env: CUSTOM_OPENAI_KEY\n",
        cli_negative_overrides=(
            "  endpoint:\n    base_url: https://gateway.example.test/v1\n    allow_custom_endpoint: false\n    credential_env: CUSTOM_OPENAI_KEY\n",
        ),
    ),
    AdapterCase(
        "OpenRouterStrictRouting",
        ProviderSettings(
            name="openrouter",
            model="deepseek/deepseek-v4.1-flash",
            base_url="https://openrouter.ai/api/v1",
            api_key="synthetic-only",
            timeout_seconds=5,
            max_output_tokens=128,
            openrouter_policy=OpenRouterRoutingPolicy("morph"),
        ),
        "OpenRouterProvider",
        cli_backend="openrouter",
        credential_env="OPENROUTER_API_KEY",
        transport_argument="_test_opener",
        approval_without_qualification=False,
    ),
)


class Transport:
    def __init__(self, case, actions):
        self.case = case
        self.clock = Clock()
        self.actions = iter(actions)
        self.calls = []

    def __call__(self, request, timeout, context):
        payload = json.loads(request.data)
        self.calls.append(
            {
                "payload": payload,
                "timeout": timeout,
                "url": request.full_url,
                "authorization": request.get_header("Authorization"),
            }
        )
        elapsed, result = next(self.actions)
        self.clock.value += elapsed
        if isinstance(result, BaseException):
            raise result
        return io.BytesIO(json.dumps(self.case.envelope(result)).encode())


class _CommonFlows:
    def setUp(self):
        denied = patch.object(
            socket.socket,
            "connect",
            side_effect=AssertionError("live network forbidden"),
        )
        denied.start()
        self.addCleanup(denied.stop)
        denied_create = patch(
            "socket.create_connection",
            side_effect=AssertionError("live network forbidden"),
        )
        denied_create.start()
        self.addCleanup(denied_create.stop)

    def fixture(self, actions):
        transport = Transport(self.case, actions)
        return self.case.create(transport), transport

    def _github_cli_fixture(self, directory, *, source_kind="issue"):
        from github_ingress_contract import CapturedGitHub

        config = Path(directory) / "config.yml"
        config.write_text(
            self.case.configuration()
            + "github:\n  writes: true\n  operation_entry: enabled\n",
            encoding="utf-8",
        )
        environment = {
            "GITHUB_TOKEN": "synthetic-read-token",
            "GITHUB_RUN_ID": "123",
            "GITHUB_RUN_ATTEMPT": "1",
            "REVIEWSENSEI_APP_ID": "7",
            "OLLAMA_API_KEY": "unselected-ollama-sentinel",
            "OPENAI_API_KEY": "unselected-openai-sentinel",
            "OPENROUTER_API_KEY": "unselected-openrouter-sentinel",
        }
        if self.case.credential_env:
            environment[self.case.credential_env] = self.case.settings.api_key
        return CapturedGitHub(source_kind), config, environment

    def test_github_cli_issue_and_inline_delivery_use_real_host_and_replay(self):
        from github_ingress_contract import ANSWER

        for source_kind in ("issue", "inline"):
            with (
                self.subTest(source_kind=source_kind),
                tempfile.TemporaryDirectory() as tmp,
            ):
                host, config, environment = self._github_cli_fixture(
                    tmp, source_kind=source_kind
                )
                transport = Transport(self.case, [(0, json.dumps({"body": ANSWER}))])
                code, output, error = host.invoke(
                    self.case, transport, config, environment
                )
                self.assertEqual(code, 0, error)
                self.assertEqual(output.strip(), "replied")
                self.assertEqual(len(transport.calls), 1)
                self.assertEqual(
                    transport.calls[0]["payload"]["model"], self.case.settings.model
                )
                if self.case.settings.openrouter_policy is not None:
                    self.assertEqual(
                        transport.calls[0]["payload"]["provider"],
                        self.case.settings.openrouter_policy.to_request_provider(),
                    )
                replies = [
                    value
                    for table in (host.comments, host.inline)
                    for value in table.values()
                    if ANSWER in value["body"]
                ]
                self.assertEqual(len(replies), 1)
                before = len(host.calls)
                code, output, error = host.invoke(
                    self.case, transport, config, environment
                )
                self.assertEqual(code, 0, error)
                self.assertEqual(output.strip(), "already_replied")
                self.assertEqual(len(transport.calls), 1)
                self.assertFalse(
                    any(
                        method == "POST" and path.endswith(("/comments", "/replies"))
                        for method, path, _ in host.calls[before:]
                    )
                )
                self.assertTrue(
                    any(
                        capability == "review_session"
                        for _, capability in host.exchanges
                    ),
                    "default operation entry must run, not fall back",
                )
                captured = (
                    json.dumps(host.calls) + json.dumps(host.exchanges) + output + error
                )
                for key in (
                    "OLLAMA_API_KEY",
                    "OPENAI_API_KEY",
                    "OPENROUTER_API_KEY",
                    self.case.credential_env,
                ):
                    if key:
                        self.assertNotIn(environment[key], captured)

    def test_github_cli_unauthorized_stale_and_disabled_replies_never_dispatch(self):
        for refusal in ("unauthorized", "stale", "draft", "closed", "disabled"):
            with self.subTest(refusal=refusal), tempfile.TemporaryDirectory() as tmp:
                host, config, environment = self._github_cli_fixture(tmp)
                if refusal == "unauthorized":
                    host.source["author_association"] = "NONE"
                elif refusal == "stale":
                    host.head = "c" * 40
                elif refusal == "draft":
                    host.draft = True
                elif refusal == "closed":
                    host.closed = True
                transport = Transport(self.case, [])
                code, output, error = host.invoke(
                    self.case,
                    transport,
                    config,
                    environment,
                    enabled=refusal != "disabled",
                )
                self.assertEqual(code, 0, error)
                self.assertIn(
                    "disabled" if refusal == "disabled" else "skipped_", output
                )
                self.assertEqual(transport.calls, [])
                self.assertEqual(host.exchanges, [])
                self.assertFalse(any(method != "GET" for method, _, _ in host.calls))

    def test_installed_host_plan_preserves_all_github_control_combinations(self):
        from review_sensei_github.cli import main as github_main

        with tempfile.TemporaryDirectory() as tmp:
            _, config, environment = self._github_cli_fixture(tmp)
            # This is the complete policy projection cross-product, not a claim
            # that every corresponding external event has been delivered.
            combinations = itertools.product(
                (False, True),
                (False, True),
                (False, True),
                ("auto-approve", "blocking", "advisory"),
                ("disabled", "proposals", "pull-requests"),
                ("none", "diagnostics"),
            )
            for (
                automatic,
                writes,
                mentions,
                reviews,
                learning,
                artifacts,
            ) in combinations:
                policy = dict(
                    automatic_reviews=automatic,
                    writes=writes,
                    mentions=mentions,
                    reviews=reviews,
                    learning=learning,
                    artifacts=artifacts,
                )
                with self.subTest(**policy):
                    config.write_text(
                        self.case.configuration()
                        + "github:\n"
                        + "".join(
                            f"  {name}: {str(value).lower() if isinstance(value, bool) else value}\n"
                            for name, value in policy.items()
                        ),
                        encoding="utf-8",
                    )
                    stdout, stderr = io.StringIO(), io.StringIO()
                    with (
                        patch.dict("os.environ", environment, clear=True),
                        redirect_stdout(stdout),
                        redirect_stderr(stderr),
                    ):
                        code = github_main(
                            ["host-plan", "--config", str(config), "--json"]
                        )
                    self.assertEqual(code, 0, stderr.getvalue())
                    plan = json.loads(stdout.getvalue())
                    self.assertEqual(plan["policy"], policy)
                    self.assertEqual(plan["backend"], self.case.cli_backend)
                    self.assertEqual(plan["model"], self.case.settings.model)
                    self.assertEqual(plan["base_url"], self.case.settings.base_url)
                    for name, value in environment.items():
                        if name.endswith("_KEY"):
                            self.assertNotIn(
                                value, stdout.getvalue() + stderr.getvalue()
                            )

    def test_public_cli_learning_generation_optout_retains_approved_learning(self):
        proposal = {
            "title": "Keep the local boundary",
            "rule": "Reject remote requests before transmission.",
            "scope": ["app.py"],
            "rationale": "The library is local-only.",
            "category": "architecture",
        }
        for enabled in (True, False):
            with self.subTest(enabled=enabled), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                _, config, environment = self._github_cli_fixture(tmp)
                # Analysis is local CLI execution, not an attested Actions job.
                environment = {
                    k: v for k, v in environment.items() if not k.startswith("GITHUB_")
                }
                diff = root / "change.patch"
                diff.write_text(DIFF, encoding="utf-8")
                directory = root / ".github/review-sensei/learnings"
                directory.mkdir(parents=True)
                (directory / "local.json").write_text(
                    json.dumps(
                        {
                            "id": "approved-local-boundary",
                            "title": "Approved local boundary",
                            "rule": "Retain the approved local-only guard.",
                            "scope": ["*"],
                            "category": "architecture",
                        }
                    ),
                    encoding="utf-8",
                )
                transport = Transport(
                    self.case,
                    [
                        (
                            0,
                            json.dumps(
                                {
                                    "summary": "Synthetic complete review.",
                                    "comments": [],
                                    "learning_proposals": [proposal],
                                }
                            ),
                        )
                    ],
                )
                stdout, stderr = io.StringIO(), io.StringIO()
                with (
                    self.case.transport_scope(transport),
                    patch.dict("os.environ", environment, clear=True),
                    redirect_stdout(stdout),
                    redirect_stderr(stderr),
                ):
                    code = main(
                        [
                            "--config",
                            str(config),
                            "--diff",
                            str(diff),
                            "--format",
                            "json",
                            "--learning-root",
                            str(root),
                            *([] if enabled else ["--no-learning-proposals"]),
                        ]
                    )
                self.assertEqual(code, 0, stderr.getvalue())
                result = json.loads(stdout.getvalue())
                self.assertEqual(len(result["learning_proposals"]), int(enabled))
                self.assertEqual(len(transport.calls), 1)
                prompt = self.case.prompt(transport.calls[0]["payload"])
                self.assertIn("approved-local-boundary", prompt)
                self.assertIn("Retain the approved local-only guard.", prompt)

    def test_github_cli_selected_credential_and_resource_refusals_do_not_publish(self):
        from github_ingress_contract import ANSWER

        if self.case.credential_env:
            with tempfile.TemporaryDirectory() as tmp:
                host, config, environment = self._github_cli_fixture(tmp)
                environment.pop(self.case.credential_env)
                transport = Transport(self.case, [])
                code, output, error = host.invoke(
                    self.case, transport, config, environment
                )
                self.assertEqual(code, 1, error)
                self.assertIn(self.case.credential_env, error)
                self.assertEqual(transport.calls, [])
                self.assertEqual(host.exchanges, [])
                self.assertEqual(host.calls, [])
        for action in ("call_limit", "cancel"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as tmp:
                host, config, environment = self._github_cli_fixture(tmp)
                config.write_text(
                    self.case.configuration()
                    + "  review_work:\n    mode: unified\n  resources:\n    max_provider_calls: 1\n"
                    + "github:\n  writes: true\n  operation_entry: enabled\n",
                    encoding="utf-8",
                )
                transport = Transport(
                    self.case,
                    [(0, KeyboardInterrupt() if action == "cancel" else "{}")],
                )
                code, output, error = host.invoke(
                    self.case, transport, config, environment
                )
                self.assertEqual(code, 130 if action == "cancel" else 1, error)
                self.assertNotIn("replied", output)
                # Once dispatched, an unaccepted result remains durably
                # unknown. This proves safe refusal, not recovery liveness.
                self.assertIn(
                    "cancelled"
                    if action == "cancel"
                    else "operation entry refused: provider_unknown",
                    error,
                )
                self.assertEqual(len(transport.calls), 1)
                self.assertTrue(
                    any(
                        method == "DELETE" and path.endswith("/reactions/99")
                        for method, path, _ in host.calls
                    )
                )
                self.assertFalse(
                    any(ANSWER in value["body"] for value in host.comments.values())
                )
                self.assertFalse(
                    any(
                        "<!-- reviewsensei:reply:v1" in value["body"]
                        for value in host.comments.values()
                    )
                )
                self.assertFalse(
                    any(
                        method == "POST" and "/reviews" in path
                        for method, path, _ in host.calls
                    )
                )
                code, output, error = host.invoke(
                    self.case, transport, config, environment
                )
                self.assertEqual(code, 1, error)
                self.assertIn("operation entry refused: provider_unknown", error)
                self.assertEqual(len(transport.calls), 1)

    def test_public_cli_canonical_settings_reach_the_real_adapter(self):
        transport = Transport(self.case, [(0, CLEAN)])
        environment = {
            "OLLAMA_API_KEY": "ambient-ollama-sentinel",
            "OPENAI_API_KEY": "ambient-openai-sentinel",
            "OPENROUTER_API_KEY": "ambient-openrouter-sentinel",
        }
        if self.case.credential_env is not None:
            environment[self.case.credential_env] = self.case.settings.api_key
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config, diff = root / "config.yml", root / "change.patch"
            config.write_text(
                self.case.configuration(),
                encoding="utf-8",
            )
            diff.write_text(DIFF, encoding="utf-8")
            stdout, stderr = io.StringIO(), io.StringIO()
            with (
                self.case.transport_scope(transport),
                patch.dict(
                    "os.environ",
                    environment,
                    clear=True,
                ),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                code = main(
                    [
                        "--config",
                        str(config),
                        "--diff",
                        str(diff),
                        "--format",
                        "json",
                        "--no-learning-proposals",
                        "--exit-semantics",
                        "operational",
                    ]
                )
            self.assertEqual(code, 0, stderr.getvalue())
            self.assertEqual(json.loads(stdout.getvalue())["review_status"], "complete")
            self.assertEqual(len(transport.calls), 1)
            self.assertEqual(
                transport.calls[0]["authorization"],
                "Bearer " + self.case.settings.api_key
                if self.case.settings.api_key is not None
                else None,
            )
            for value in environment.values():
                self.assertNotIn(value, stdout.getvalue() + stderr.getvalue())
            self.assertEqual(
                transport.calls[0]["payload"]["model"], self.case.settings.model
            )
            self.assertTrue(
                transport.calls[0]["url"].startswith(self.case.settings.base_url + "/")
            )
            if self.case.settings.openrouter_policy is not None:
                self.assertEqual(
                    transport.calls[0]["payload"]["provider"],
                    self.case.settings.openrouter_policy.to_request_provider(),
                )
            for rejected in self.case.cli_negative_overrides:
                config.write_text(
                    self.case.configuration(advanced_yaml=rejected),
                    encoding="utf-8",
                )
                with (
                    self.case.transport_scope(transport),
                    patch.dict("os.environ", environment, clear=True),
                    redirect_stdout(io.StringIO()),
                    redirect_stderr(io.StringIO()),
                ):
                    code = main(
                        [
                            "--config",
                            str(config),
                            "--diff",
                            str(diff),
                            "--no-learning-proposals",
                        ]
                    )
                self.assertEqual(code, 2)
                self.assertEqual(len(transport.calls), 1)

    def run_review(
        self,
        actions,
        *,
        mode="legacy",
        stages=None,
        calls=8,
        timeout=1000,
        prompt_bytes=262144,
    ):
        provider, transport = self.fixture(actions)
        result = ReviewService(
            provider, stages=stages, work_budgets=ReviewWorkBudgets(mode=mode)
        ).run(
            ReviewRequest(diff=DIFF),
            budget=ResourceBudget.create(
                timeout_ms=timeout,
                max_provider_calls=calls,
                max_retry_attempts=2,
                max_output_bytes=16384,
                max_prompt_bytes=prompt_bytes,
            ),
            monotonic=transport.clock,
            sleeper=transport.clock.sleep,
        )
        return result, transport

    def assert_withheld(self, run):
        if run.result is not None:
            self.assertNotEqual(run.result.review_status, "complete")
            self.assertFalse(
                evaluate_approval_facts(
                    approval_facts_from_result(
                        run.result, enabled=True, app_authored=False
                    )
                ).approved
            )
            self.assertNotEqual(
                check_outcome_for_result(run.result, policy="blocking").conclusion,
                "success",
            )

    def test_complete_clean_and_blocking_results_agree_with_approval_and_check(self):
        blocker = json.dumps(
            {
                "summary": "Synthetic blocker.",
                "comments": [
                    {
                        "path": "app.py",
                        "line": 1,
                        "body": "Authorization failure; reject unauthorized requests.",
                        "blocking": True,
                        "severity": "high",
                        "defect_kind": "authz-failure",
                    }
                ],
            }
        )
        for mode in ("legacy", "unified"):
            for text, approved, conclusion in (
                (CLEAN, True, "success"),
                (blocker, False, "failure"),
            ):
                with self.subTest(mode=mode, approved=approved):
                    run, transport = self.run_review([(0.125, text)], mode=mode)
                    self.assertEqual(run.outcome.status, "reviewed")
                    self.assertEqual(run.result.review_status, "complete")
                    self.assertEqual(run.result.provider, self.case.settings.name)
                    self.assertEqual(run.outcome.provider_calls, 1)
                    self.assertEqual(len(transport.calls), 1)
                    self.assertEqual(
                        transport.calls[0]["payload"]["model"], self.case.settings.model
                    )
                    self.assertLessEqual(
                        self.case.output_tokens(transport.calls[0]["payload"]), 128
                    )
                    facts = approval_facts_from_result(
                        run.result,
                        enabled=True,
                        app_authored=False,
                        has_open_review_threads=False,
                    )
                    self.assertEqual(
                        evaluate_approval_facts(facts).approved,
                        approved and self.case.approval_without_qualification,
                    )
                    self.assertEqual(
                        check_outcome_for_result(
                            run.result, policy="blocking"
                        ).conclusion,
                        conclusion,
                    )

    def test_stage_calls_share_original_deadline_and_transport_output_cap(self):
        stages = tuple(
            Stage(name=name, prompt_template="{diff}", outputs=("summary", "comments"))
            for name in ("first", "second")
        )
        for mode in ("legacy", "unified"):
            with self.subTest(mode=mode):
                run, transport = self.run_review(
                    [(0.25, CLEAN), (0.25, CLEAN)], mode=mode, stages=stages
                )
                self.assertEqual(run.outcome.status, "reviewed")
                self.assertEqual(
                    [call["timeout"] for call in transport.calls], [1.0, 0.75]
                )
                self.assertTrue(
                    all(
                        self.case.output_tokens(call["payload"]) <= 128
                        for call in transport.calls
                    )
                )

    def test_real_transport_retry_and_json_correction_share_original_allowance(self):
        for mode in ("legacy", "unified"):
            with self.subTest(mode=mode):
                run, transport = self.run_review(
                    [
                        (0.25, TimeoutError("synthetic-private-error")),
                        (0.25, "{"),
                        (0.125, CLEAN),
                    ],
                    mode=mode,
                )
                self.assertEqual(run.outcome.status, "reviewed")
                self.assertEqual(
                    [call["timeout"] for call in transport.calls], [1.0, 0.75, 0.5]
                )
                self.assertEqual(run.outcome.provider_calls, 3)
                self.assertEqual(
                    (run.outcome.retry_attempts, run.outcome.structural_retries), (1, 1)
                )
                self.assertNotIn(
                    "synthetic-private-error", json.dumps(run.outcome.to_dict())
                )

    def test_retry_and_correction_cannot_bypass_provider_call_ceiling(self):
        for mode in ("legacy", "unified"):
            for value in (TimeoutError("synthetic"), "{"):
                with self.subTest(mode=mode, kind=type(value).__name__):
                    run, transport = self.run_review(
                        [(0.125, value)], mode=mode, calls=1
                    )
                    self.assertEqual(len(transport.calls), 1)
                    self.assertEqual(run.outcome.provider_calls, 1)
                    self.assert_withheld(run)
                    self.assertNotEqual(run.outcome.status, "reviewed")

    def test_expired_or_exhausted_deadline_has_no_fresh_dispatch(self):
        for mode in ("legacy", "unified"):
            with self.subTest(mode=mode):
                run, transport = self.run_review([], mode=mode, timeout=0)
                self.assertEqual(transport.calls, [])
                self.assert_withheld(run)
                run, transport = self.run_review(
                    [(1.0, TimeoutError("synthetic-private-error"))], mode=mode
                )
                self.assertEqual(len(transport.calls), 1)
                self.assertEqual(run.outcome.retry_attempts, 0)
                self.assert_withheld(run)

    def test_prompt_budget_exhaustion_refuses_before_real_transport(self):
        for mode in ("legacy", "unified"):
            with self.subTest(mode=mode):
                run, transport = self.run_review([], mode=mode, prompt_bytes=1)
                self.assertEqual(transport.calls, [])
                self.assertNotEqual(run.outcome.status, "reviewed")
                self.assert_withheld(run)

    def test_invalid_review_output_stays_blocked_and_is_not_echoed_to_correction(self):
        secret = "SYNTHETIC_INVALID_RESPONSE_SENTINEL"
        for mode in ("legacy", "unified"):
            with self.subTest(mode=mode):
                run, transport = self.run_review(
                    [(0.125, secret), (0.125, secret)], mode=mode
                )
                self.assertEqual(len(transport.calls), 2)
                self.assertEqual(run.outcome.structural_retries, 1)
                self.assertNotIn(
                    secret, self.case.prompt(transport.calls[1]["payload"])
                )
                self.assertNotIn(secret, json.dumps(run.outcome.to_dict()))
                self.assert_withheld(run)

    def test_keyboard_interrupt_does_not_become_clean_review_or_retry(self):
        for mode in ("legacy", "unified"):
            provider, transport = self.fixture([(0, KeyboardInterrupt())])
            with self.subTest(mode=mode), self.assertRaises(KeyboardInterrupt):
                ReviewService(provider, work_budgets=ReviewWorkBudgets(mode=mode)).run(
                    ReviewRequest(diff=DIFF)
                )
            self.assertEqual(len(transport.calls), 1)

    def test_full_fallback_from_real_discovery_retains_omitted_concern(self):
        comment = {
            "path": "app.py",
            "line": 1,
            "body": "Authorization failure; reject unauthorized requests.",
            "blocking": True,
            "severity": "high",
            "defect_kind": "authz-failure",
        }
        for mode in ("legacy", "unified"):
            with self.subTest(mode=mode):
                prior, _ = self.run_review(
                    [
                        (
                            0,
                            json.dumps(
                                {"summary": "Synthetic blocker", "comments": [comment]}
                            ),
                        )
                    ],
                    mode=mode,
                )
                key = ReviewContextCacheKey(
                    repository="example/repo",
                    pull_request=1,
                    base_sha=BASE,
                    head_sha=HEAD,
                    engine="synthetic",
                    model=self.case.settings.model,
                    profile="default",
                    stage_digest="a" * 64,
                    context_digest="b" * 64,
                    learning_digest="c" * 64,
                )
                policy = ReviewConvergencePolicy(mode="merge-focused")
                baseline = baseline_from_review(
                    prior.result, cache_key=key, policy=policy
                )
                scope = plan_verification_scope(
                    policy=policy,
                    baseline=baseline,
                    current_key=replace(key, base_sha="c" * 40),
                    changed_paths=("app.py",),
                )
                self.assertEqual(scope.coverage_mode, "fallback-full")
                self.assertIsNone(scope.incremental)
                refreshed, _ = self.run_review([(0, CLEAN)], mode=mode)
                reconciled = reconcile_overflow_review(
                    refreshed.result, baseline, coverage_mode=scope.coverage_mode
                )
                self.assertEqual(reconciled.review_status, "partial")
                self.assertIn(
                    baseline.findings[0].fingerprint,
                    [
                        item.fingerprint
                        for item in reconciled.finding_lifecycles
                        if item.state == "uncertain"
                    ],
                )
                self.assertFalse(
                    evaluate_approval_facts(
                        approval_facts_from_result(
                            reconciled, enabled=True, app_authored=False
                        )
                    ).approved
                )
                self.assertEqual(
                    check_outcome_for_result(reconciled, policy="blocking").conclusion,
                    "action_required",
                )

    def test_actual_adapter_does_not_inherit_unverified_expanded_capability(self):
        provider, transport = self.fixture([])
        self.assertIsNone(getattr(provider, "completion_contract", None))
        capability = ProviderCapabilities(
            context_tokens=200000,
            max_output_tokens=128,
            qualified=True,
            provider_name=provider.name,
            model=provider.model,
            endpoint=provider.base_url
            + ("/generate" if self.case.ollama_envelope else "/chat/completions"),
            completion_contract="bounded-complete-text-v1",
        )
        with self.assertRaises(ReviewInputError):
            capability.require_provider(provider, model=provider.model)
        with self.assertRaises(ReviewInputError):
            ReviewService(
                provider,
                work_budgets=ReviewWorkBudgets(mode="unified"),
                capabilities=capability,
            ).run(ReviewRequest(diff=DIFF, base_sha=BASE, head_sha=HEAD))
        self.assertEqual(transport.calls, [])

    def test_accepted_hosted_computation_replays_real_adapter_result_without_dispatch(
        self,
    ):
        try:
            from test_operation_entry import MemoryComments
        except ModuleNotFoundError:
            from tests.test_operation_entry import MemoryComments
        for mode in ("legacy", "unified"):
            with self.subTest(mode=mode):
                provider, transport = self.fixture([(0, CLEAN)])
                comments = MemoryComments()

                def invoke():
                    return call_operation_trigger(
                        GitHubCommentOperationStore(comments, app_user_id=42),
                        trigger="full-review",
                        repository_id=7,
                        pull_request=11,
                        attestation_digest="a" * 64,
                        run_id="synthetic-run",
                        run_attempt="1",
                        app_id=99,
                        reservation_id="synthetic-reservation",
                        delivery_id="synthetic-delivery",
                        grant="synthetic-grant",
                        execute=lambda: ReviewService(
                            provider, work_budgets=ReviewWorkBudgets(mode=mode)
                        ).run(ReviewRequest(diff=DIFF)),
                        encode=analysis_payload,
                        decode=lambda payload: ReviewResult.from_dict(
                            json.loads(payload)
                        ),
                        parts=ResultPartStore(comments, app_user_id=42),
                    )

                first = invoke()
                before = GitHubCommentOperationStore(comments, app_user_id=42)._live()
                replay = invoke()
                self.assertEqual(first.result.to_dict(), replay.to_dict())
                self.assertEqual(len(transport.calls), 1)
                self.assertEqual(
                    GitHubCommentOperationStore(comments, app_user_id=42)._live(),
                    before,
                )

    def test_human_reassessment_correction_valid_evidence_and_stale_refusal(self):
        clean, _ = self.run_review([(0, CLEAN)])
        human = replace(
            clean.result,
            comments=(
                ReviewComment(
                    "app.py",
                    1,
                    "Confirm the local-only boundary.",
                    needs_human=True,
                    effective_blocking=False,
                ),
            ),
        )
        eligibility = approval_eligibility_from_result(
            human, head_sha=HEAD, base_sha=BASE, enabled=True, app_authored=False
        )
        pending = eligibility.human_review
        fingerprint = pending.pending[0].fingerprint
        response = json.dumps(
            {
                "body": "The explanation matches the bounded diff.",
                "assessments": [
                    {
                        "fingerprint": fingerprint,
                        "decision": "dismissed",
                        "rationale": "The cited code rejects remote requests before sending input.",
                        "human_evidence": "This path is intentionally local-only",
                        "diff_evidence": "reject_remote_requests(data)",
                    }
                ],
            }
        )
        provider, transport = self.fixture([(0.25, "{"), (0.25, response)])
        tracker = ResourceBudgetTracker(
            ResourceBudget.create(timeout_ms=1000), monotonic=transport.clock
        )
        context = ConversationContext(
            base_sha=BASE, head_sha=HEAD, diff_context="path=app.py\n" + DIFF
        )
        reply = HumanAssessmentService(provider).reply(
            context=context,
            pending=pending,
            source_body=SOURCE,
            model=provider.model,
            tracker=tracker,
        )
        self.assertEqual([call["timeout"] for call in transport.calls], [1.0, 0.75])
        resolved = pending.apply(reply.decisions)
        self.assertEqual(resolved.pending, ())
        self.assertFalse(
            eligibility.evaluate(
                app_authored=False, has_open_review_threads=False
            ).approved
        )
        updated = replace(
            eligibility,
            human_review=resolved,
            facts=replace(
                eligibility.facts,
                has_human_adjudication_findings=bool(resolved.pending),
            ),
        )
        restored = ReviewApprovalEligibility.from_dict(
            json.loads(json.dumps(updated.to_dict()))
        )
        self.assertEqual(
            restored.evaluate(
                app_authored=False, has_open_review_threads=False
            ).approved,
            self.case.approval_without_qualification,
        )
        self.assertEqual(len(transport.calls), 2)
        with self.assertRaises(ReviewInputError):
            approval_eligibility_from_result(
                human,
                head_sha="c" * 40,
                base_sha=BASE,
                enabled=True,
                app_authored=False,
                retained_eligibility=restored,
            )
        stale_provider, stale_transport = self.fixture([])
        with self.assertRaises(ReviewInputError):
            HumanAssessmentService(stale_provider).reply(
                context=replace(context, base_sha="d" * 40),
                pending=pending,
                source_body=SOURCE,
            )
        self.assertEqual(stale_transport.calls, [])

    def test_ordinary_discussion_uses_same_adapter_and_bounded_correction(self):
        provider, transport = self.fixture(
            [(0, "{"), (0, '{"body":"Synthetic discussion reply.","resolve":false}')]
        )
        result = ConversationService(provider).reply(
            ConversationContext(base_sha=BASE, head_sha=HEAD, diff_context=DIFF),
            model=provider.model,
        )
        self.assertFalse(result.resolve)
        self.assertEqual(result.body, "Synthetic discussion reply.")
        self.assertEqual(len(transport.calls), 2)


FLOW_TEST_CASES = tuple(
    type(
        case.name + "CommonFlowTests",
        (_CommonFlows, unittest.TestCase),
        {"case": case, "__module__": __name__},
    )
    for case in ADAPTER_CASES
)
# The sharded checkout runner reloads exact IDs in child processes. Each class
# must be importable by that ID, not only reachable from load_tests' locals.
for _case_type in FLOW_TEST_CASES:
    globals()[_case_type.__name__] = _case_type


def load_common_flow_tests(loader):
    return unittest.TestSuite(
        loader.loadTestsFromTestCase(cls)
        for cls in (*FLOW_TEST_CASES, OllamaApprovedSettingsTests)
    )


class OllamaApprovedSettingsTests(unittest.TestCase):
    def test_approved_cloud_model_settings_use_real_registry_and_transport(self):
        case = ADAPTER_CASES[0]
        for model in ("deepseek-v4.1-flash:cloud", "glm-5.3:cloud"):
            with self.subTest(model=model):
                transport = Transport(case, [(0, CLEAN)])
                provider = case.create(transport, model=model)
                run = ReviewService(provider).run(ReviewRequest(diff=DIFF))
                self.assertEqual(run.result.review_status, "complete")
                self.assertEqual(transport.calls[0]["payload"]["model"], model)
                self.assertIsNone(provider.completion_contract)

    def test_strict_ollama_completion_is_an_explicit_library_opt_in(self):
        case = ADAPTER_CASES[0]
        transport = Transport(case, [(0, CLEAN)])
        strict = OllamaProvider(
            base_url=case.settings.base_url,
            model=case.settings.model,
            api_key="synthetic-only",
            max_output_tokens=128,
            require_completion_metadata=True,
            allow_model_override=False,
            opener=transport,
        )
        self.assertEqual(strict.completion_contract, "bounded-complete-text-v1")
        run = ReviewService(strict).run(ReviewRequest(diff=DIFF))
        self.assertEqual(run.result.review_status, "complete")
        calls = []

        def incomplete(request, timeout, context):
            calls.append(request)
            return io.BytesIO(json.dumps({"response": CLEAN}).encode())

        refused = OllamaProvider(
            base_url=case.settings.base_url,
            model=case.settings.model,
            api_key="synthetic-only",
            max_output_tokens=128,
            require_completion_metadata=True,
            allow_model_override=False,
            opener=incomplete,
        )
        run = ReviewService(refused).run(ReviewRequest(diff=DIFF))
        self.assertIsNone(run.result)
        self.assertIsInstance(run.error, ProviderError)
        self.assertEqual(len(calls), 1)
