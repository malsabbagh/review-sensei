"""Installed adapter positive control using synthetic packaged-corpus output.

No remote provider or real support record is used or claimed by this test.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

_TESTS_ROOT = Path(__file__).resolve().parents[1]
if str(_TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TESTS_ROOT))

from packaging_guard import assert_distribution_import  # noqa: E402

assert_distribution_import()

from fake_github_http import json_response, make_http  # noqa: E402
from review_sensei_github import (  # noqa: E402
    ReviewPublisher,
    TrustedOpenRouterQualification,
)
from review_sensei_github.publication import (  # noqa: E402
    ReviewApprovalFinalizer,
    approval_eligibility_from_body,
)

from review_sensei.evaluation import (  # noqa: E402
    configuration_digest,
    engine_digest,
    evaluate_live,
    load_corpus,
    prompt_digest,
)
from review_sensei.models import (  # noqa: E402
    ProviderResponse,
    ReviewResult,
    build_transaction_configuration_context,
    transaction_stage_identity,
)
from review_sensei.openrouter_qualification import (  # noqa: E402
    CURRENT_HOSTED_OPENROUTER_QUALIFICATION_TARGET as TARGET,
)
from review_sensei.openrouter_qualification import (  # noqa: E402
    qualification_record_from_reports,
)
from review_sensei.service import DEFAULT_STAGES  # noqa: E402


class InstalledRuntimeQualificationTests(unittest.TestCase):
    def test_checked_packaged_corpus_evidence_binds_publisher_and_delayed_finalizer(
        self,
    ):
        corpus = load_corpus(
            Path(__file__).resolve().parents[2] / "evaluation/v1/corpus.json"
        )
        quality = [
            case for case in corpus.document["cases"] if case["kind"] == "quality"
        ]

        class SyntheticProvider:
            name = "openrouter"
            model = TARGET.model
            base_url = TARGET.base_url
            routing_policy = TARGET.routing_policy()

            def __init__(self):
                self.calls = 0

            def complete(self, request):
                case = quality[self.calls]
                self.calls += 1
                return ProviderResponse(
                    corpus.read_asset(
                        case["response_path"], maximum=1048576, label="synthetic oracle"
                    ),
                    self.name,
                    model=self.model,
                )

        reports = [
            evaluate_live(
                corpus,
                SyntheticProvider(),
                provider_version="synthetic-control",
                model=TARGET.model,
                endpoint_scope="remote",
            )
            for _ in range(3)
        ]
        self.assertTrue(all(report["passed"] for report in reports))
        artifacts = [json.dumps(report).encode() for report in reports]
        record = qualification_record_from_reports(
            reports,
            target=TARGET,
            observed_revision="synthetic-control",
            reproducibility={"synthetic": True},
            evaluated_at="2026-10-01T00:00:00Z",
            report_artifacts=artifacts,
        )
        stages, categories = transaction_stage_identity(DEFAULT_STAGES)
        context = build_transaction_configuration_context(
            provider={
                "name": "openrouter",
                "profile": None,
                "base_url": TARGET.base_url,
                "timeout_seconds": 60,
                "max_output_tokens": 4096,
                "allow_custom_endpoint": False,
                "openrouter_policy": dict(TARGET.routing_policy().identity_fields()),
            },
            model=TARGET.model,
            stages=stages,
            category_policy=categories,
            publication_mode="merge-focused",
            orchestration_enabled=False,
        )
        # Expectations come from the selected installed engine/corpus/configuration,
        # independently of the record's claims or report run fields.
        authority = TrustedOpenRouterQualification(
            qualification_document=json.dumps(record.to_dict()).encode(),
            report_artifacts=artifacts,
            expected_target=TARGET,
            expected_evaluated_at="2026-10-01T00:00:00Z",
            expected_reproducibility={"synthetic": True},
            expected_engine_digest=engine_digest(),
            expected_prompt_digest=prompt_digest(),
            expected_corpus_digest=corpus.digest,
            expected_evaluation_configuration_digest=configuration_digest(
                corpus,
                "live",
                "openrouter",
                TARGET.model,
                openrouter_policy=TARGET.routing_policy().identity_fields(),
            ),
            runtime_configuration=context,
        )
        head, base = "b" * 40, "a" * 40
        pr = {
            "state": "open",
            "draft": False,
            "user": {"login": "alice", "type": "User"},
            "head": {"sha": head, "repo": {"full_name": "owner/repo", "fork": False}},
            "base": {
                "ref": "main",
                "sha": base,
                "repo": {"id": 1, "full_name": "owner/repo", "fork": False},
            },
        }
        threads = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "nodes": [],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            }
        }
        http, calls = make_http(
            [
                json_response(pr),
                json_response([]),
                json_response(pr),
                json_response({"id": 5}),
                json_response(pr),
                json_response(threads),
                json_response(pr),
                json_response([]),
                json_response({"id": 6}),
            ],
            review_readback=True,
        )
        result = ReviewResult(
            summary="Synthetic clean control.",
            comments=(),
            provider="openrouter",
            model=TARGET.model,
            review_status="complete",
        )
        published = ReviewPublisher(
            http=http, qualification_authority=authority
        ).publish(
            token="synthetic",
            repository="owner/repo",
            repository_id=1,
            pull_request=2,
            head_sha=head,
            base_branch="main",
            base_sha=base,
            result=result,
            diff="diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-a\n+b\n",
            app_slug="reviewsensei[bot]",
            configuration_context=context,
        )
        self.assertEqual(published.status, "published")
        writes = [
            json.loads(raw)
            for method, url, raw in calls
            if method == "POST" and url.endswith("/reviews")
        ]
        self.assertEqual([item["event"] for item in writes], ["COMMENT", "APPROVE"])
        eligibility = approval_eligibility_from_body(writes[0]["body"])
        self.assertEqual(eligibility.facts.to_dict()["schema_version"], "2")
        review = {
            "body": writes[0]["body"],
            "commit_id": head,
            "state": "COMMENTED",
            "user": {"login": "reviewsensei[bot]"},
        }
        http, calls = make_http(
            [
                json_response(pr),
                json_response(threads),
                json_response(pr),
                json_response([review]),
                json_response({"id": 7}),
            ]
        )
        args = dict(
            token="synthetic",
            repository="owner/repo",
            pull_request=2,
            head_sha=head,
            app_slug="reviewsensei[bot]",
            eligibility=eligibility,
            require_persisted=True,
        )
        self.assertEqual(
            ReviewApprovalFinalizer(http=http, qualification_authority=authority)
            .finalize(**args)
            .status,
            "approved",
        )
        http, calls = make_http([])
        self.assertEqual(
            ReviewApprovalFinalizer(http=http).finalize(**args).status,
            "approval_withheld",
        )
        self.assertEqual(calls, [])
