"""Consumer readback of complete authority, including literal Git paths."""

import json
import tempfile
import unittest
from pathlib import Path

from review_sensei.budgets import ReviewWorkBudgets
from review_sensei.hosting.github.observed import restored_compatible_baseline
from review_sensei.models import ConversationFinding, ReviewRequest
from review_sensei.service import ReviewService
from review_sensei.session import LocalSessionLedger
from tests import test_review_transaction as fixture
from tests.test_authenticated_partitions import checkpoint
from tests.test_shared_document_prompt import DIFF, Provider


class PartitionConsumerTests(unittest.TestCase):
    def test_observed_restart_reconstructs_complete_100_250_inventory(self):
        for count in (100, 250):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as directory:
                baseline, _ = checkpoint(
                    LocalSessionLedger(Path(directory), enable_partition_writes=True),
                    count,
                )
                reader = LocalSessionLedger(Path(directory))
                record = reader.load(fixture.IDENTITY, now=fixture.NOW).record
                history = record.convergence_history
                self.assertIsNone(
                    restored_compatible_baseline(
                        history, current_key=baseline.cache_key, policy=fixture.POLICY
                    )
                )
                restored = restored_compatible_baseline(
                    history,
                    current_key=baseline.cache_key,
                    policy=fixture.POLICY,
                    ledger=reader,
                    record=record,
                )
                self.assertEqual(restored, baseline)
                self.assertEqual(len(restored.findings), count)
                self.assertEqual(record.convergence_history, history)
                part = next(Path(directory).glob(".evidence/*/*/*"))
                part.unlink()
                self.assertIsNone(
                    restored_compatible_baseline(
                        history,
                        current_key=baseline.cache_key,
                        policy=fixture.POLICY,
                        ledger=reader,
                        record=record,
                    )
                )

    def test_full_review_retains_literal_glob_path_without_pattern_expansion(self):
        path = "src/[draft]*.py"
        provider = Provider()

        def complete(request):
            provider.requests.append(request)
            from review_sensei.models import ProviderResponse

            return ProviderResponse(
                json.dumps(
                    {
                        "summary": "Reviewed literal file.",
                        "comments": [
                            {"path": path, "line": 1, "body": "Retain this exact file."}
                        ],
                    }
                ),
                provider.name,
                provider.model,
            )

        provider.complete = complete
        run = ReviewService(
            provider, work_budgets=ReviewWorkBudgets(mode="unified")
        ).run(ReviewRequest(diff=DIFF.replace("src/app.py", path)))
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(run.result.comments[0].path, path)
        self.assertEqual(ConversationFinding("Exact file.", path).path, path)
