"""Installed-wheel OpenRouter contracts using synthetic response bytes only."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_TESTS_ROOT = Path(__file__).resolve().parents[1]
if str(_TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TESTS_ROOT))

from packaging_guard import assert_distribution_import  # noqa: E402

assert_distribution_import()

from review_sensei import ProviderRequest, ProviderTokenUsage  # noqa: E402
from review_sensei.cli import main  # noqa: E402
from review_sensei.errors import ReviewInputError  # noqa: E402
from review_sensei.evaluation import load_corpus  # noqa: E402
from review_sensei.openrouter_qualification import (  # noqa: E402
    CURRENT_HOSTED_OPENROUTER_QUALIFICATION_TARGET,
    load_supported_openrouter_qualification,
    qualification_record_from_reports,
)
from review_sensei.provider_config import hosted_openrouter_upstream  # noqa: E402
from review_sensei.providers.openrouter import (  # noqa: E402
    OpenRouterProvider,
    OpenRouterRoutingPolicy,
)
from review_sensei.schemas import load_schema  # noqa: E402


class _Response:
    def __init__(self, value):
        self._body = json.dumps(value).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size):
        body, self._body = self._body[:size], self._body[size:]
        return body


class InstalledOpenRouterMetadataContractTests(unittest.TestCase):
    def test_installed_packaged_live_oracle_preserves_distinct_control_identity(self):
        target = CURRENT_HOSTED_OPENROUTER_QUALIFICATION_TARGET
        corpus_path = Path(__file__).resolve().parents[2] / "evaluation/v1/corpus.json"
        corpus = load_corpus(corpus_path)
        responses = [
            corpus.read_asset(
                case["response_path"], maximum=1048576, label="synthetic oracle"
            )
            for case in corpus.document["cases"]
            if case["kind"] == "quality"
        ]
        requests = []

        def opener(request, timeout):
            content = responses[len(requests) % len(responses)]
            requests.append(json.loads(request.data))
            return _Response({"choices": [{"message": {"content": content}}]})

        reports, artifacts = [], []
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.dict(
                    "os.environ",
                    {
                        "OPENROUTER_API_KEY": "synthetic",
                        "OPENROUTER_UPSTREAM_PROVIDER": "morph",
                    },
                    clear=True,
                ),
                patch(
                    "review_sensei.providers.openrouter.build_opener",
                    return_value=SimpleNamespace(open=opener),
                ),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                for index in range(3):
                    output = Path(tmp) / f"synthetic-{index}.json"
                    status = main(
                        [
                            "evaluate",
                            "--mode",
                            "live",
                            "--corpus",
                            str(corpus_path),
                            "--provider",
                            "openrouter",
                            "--base-url",
                            target.base_url,
                            "--model",
                            target.model,
                            "--provider-version",
                            "synthetic-attestation",
                            "--allow-live-model",
                            "--allow-data-egress",
                            "--output",
                            str(output),
                        ]
                    )
                    self.assertEqual(status, 0)
                    artifacts.append(output.read_bytes())
                    reports.append(json.loads(artifacts[-1]))
        self.assertEqual(len(requests), 3 * len(responses))
        self.assertEqual(len({report["run"]["invocation_id"] for report in reports}), 3)
        for report in reports:
            self.assertEqual(report["run"]["provider"], "openrouter")
            self.assertEqual(
                report["evaluation_control"]["fixture"]["provider"], "fixture"
            )
            self.assertEqual(report["metrics"]["provider_calls"], len(responses))
        # Three independently invoked synthetic controls test the gate contract;
        # this ephemeral positive is not evidence of real provider qualification.
        record = qualification_record_from_reports(
            reports,
            target=target,
            observed_revision="synthetic-attestation",
            reproducibility={"synthetic": True},
            evaluated_at="2026-10-01T00:00:00Z",
            report_artifacts=artifacts,
            status="supported",
        )
        checked = load_supported_openrouter_qualification(
            record.to_dict(),
            reports,
            report_artifacts=artifacts,
            expected_target=target,
            expected_evaluated_at="2026-10-01T00:00:00Z",
            expected_reproducibility={"synthetic": True},
        )
        self.assertEqual(checked.status, "supported")

    def test_installed_protocol_metadata_routing_and_eligibility(self):
        captured = []

        def opener(request, timeout, context):
            captured.append(json.loads(request.data))
            return _Response(
                {
                    "model": "observed/model",
                    "provider": "Synthetic Upstream",
                    "system_fingerprint": "fp_synthetic",
                    "choices": [{"message": {"content": "ok"}}],
                    "usage": {"prompt_tokens": 2, "completion_tokens": 1},
                }
            )

        policy = OpenRouterRoutingPolicy("deepinfra/turbo")
        provider = OpenRouterProvider(
            model="requested/model",
            api_key="synthetic",
            routing_policy=policy,
            _test_opener=opener,
        )
        response = provider.complete(ProviderRequest(prompt="synthetic"))
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["provider"], policy.to_request_provider())
        self.assertEqual(response.model, "requested/model")
        self.assertEqual(response.observed_model, "observed/model")
        self.assertEqual(response.observed_upstream, "Synthetic Upstream")
        self.assertEqual(response.revision, "fp_synthetic")
        self.assertEqual(response.revision_source, "provider-system-fingerprint")
        self.assertEqual(response.token_usage, ProviderTokenUsage(2, 1))
        self.assertIsNone(response.token_usage.total_tokens)
        target = CURRENT_HOSTED_OPENROUTER_QUALIFICATION_TARGET
        self.assertEqual(
            hosted_openrouter_upstream(target.model), target.upstream_provider
        )
        metrics = load_schema("evaluation-report")["properties"]["metrics"][
            "properties"
        ]
        self.assertIn("provider_reported_usage", metrics)

    def test_installed_live_report_binds_actual_routing_and_endpoint(self):
        target = CURRENT_HOSTED_OPENROUTER_QUALIFICATION_TARGET
        captured = []

        def opener(request, timeout):
            captured.append(json.loads(request.data))
            return _Response(
                {
                    "choices": [
                        {"message": {"content": '{"summary":"ok","comments":[]}'}}
                    ]
                }
            )

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "report.json"
            corpus = Path(__file__).resolve().parents[2] / "evaluation/v1/corpus.json"
            with (
                patch.dict(
                    "os.environ",
                    {
                        "OPENROUTER_API_KEY": "synthetic",
                        "OPENROUTER_UPSTREAM_PROVIDER": "morph",
                    },
                    clear=True,
                ),
                patch(
                    "review_sensei.providers.openrouter.build_opener",
                    return_value=SimpleNamespace(open=opener),
                ),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                status = main(
                    [
                        "evaluate",
                        "--mode",
                        "live",
                        "--corpus",
                        str(corpus),
                        "--provider",
                        "openrouter",
                        "--base-url",
                        target.base_url,
                        "--model",
                        target.model,
                        "--provider-version",
                        "synthetic-attestation",
                        "--allow-live-model",
                        "--allow-data-egress",
                        "--output",
                        str(output),
                    ]
                )
            # Synthetic clean responses are not passing quality evidence.
            self.assertEqual(status, 1)
            report = json.loads(output.read_text())
        self.assertFalse(report["passed"])
        self.assertTrue(any(case["status"] == "skipped" for case in report["cases"]))
        self.assertEqual(report["deterministic"]["expected_contract_rejection_rate"], 1)
        self.assertEqual(report["quality"]["expected_finding_recall"], 0)
        self.assertEqual(report["evaluation_control"]["fixture"]["provider"], "fixture")
        self.assertGreater(len(captured), 0)
        self.assertTrue(
            all(
                request["provider"] == target.routing_policy().to_request_provider()
                for request in captured
            )
        )
        self.assertEqual(report["run"]["openrouter_base_url"], target.base_url)
        self.assertEqual(
            report["run"]["openrouter_routing_policy_digest"],
            target.routing_policy_digest(),
        )
        del report["run"]["openrouter_routing_policy_digest"]
        with self.assertRaisesRegex(ReviewInputError, "routing"):
            qualification_record_from_reports(
                [report],
                target=target,
                observed_revision="synthetic-attestation",
                reproducibility={},
                evaluated_at="2026-10-18T00:00:00Z",
            )

    def test_installed_canonical_configuration_preserves_endpoint_in_real_adapter(self):
        requests = []

        def opener(request, timeout):
            requests.append(json.loads(request.data))
            return _Response(
                {
                    "choices": [
                        {
                            "message": {
                                "content": '{"summary":"Synthetic clean","comments":[]}'
                            }
                        }
                    ]
                }
            )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / ".reviewsensei.yml"
            diff = root / "change.patch"
            config.write_text(
                "schema: 1\ninference:\n  backend: openrouter\nadvanced:\n  routing:\n    upstream_provider: morph/variant\n  egress:\n    allow_data_egress: true\n",
                encoding="utf-8",
            )
            diff.write_text(
                "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-x = 0\n+x = 1\n",
                encoding="utf-8",
                newline="\n",
            )
            with (
                patch.dict(
                    "os.environ", {"OPENROUTER_API_KEY": "synthetic"}, clear=True
                ),
                patch(
                    "review_sensei.providers.openrouter.build_opener",
                    return_value=SimpleNamespace(open=opener),
                ),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                status = main(
                    [
                        "--diff",
                        str(diff),
                        "--config",
                        str(config),
                        "--no-learning-proposals",
                    ]
                )
        self.assertEqual(status, 0)
        self.assertEqual(len(requests), 1)
        self.assertEqual(
            requests[0]["provider"],
            OpenRouterRoutingPolicy("morph/variant").to_request_provider(),
        )
