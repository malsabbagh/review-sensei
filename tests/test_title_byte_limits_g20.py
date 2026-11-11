"""G20: PR title UTF-8 byte limits across routing, hosted bootstrap (256B), and core (4,096B)."""

from __future__ import annotations

import json
import tempfile
import unittest
from typing import Any

from fake_github_http import json_response
from review_sensei_github.session_ledger import parse_session_comment
from test_action_reply_entry import ReplyEntryFixture
from test_action_run import CommandEntryFixture
from test_api_work_persistence import BASE, HEAD, IDENTITY
from test_review_run import EntryFixture

from review_sensei.errors import ReviewInputError
from review_sensei.models import ProviderRequest, ProviderResponse, ReviewRequest
from review_sensei.operational_boundaries import (
    DIRECT_CORE_MAX_TITLE_BYTES,
    HOSTED_BOOTSTRAP_MAX_TITLE_BYTES,
    TitleByteLimitEvaluation,
    evaluate_title_byte_limit,
)
from review_sensei.service import ReviewService
from review_sensei.stages import Stage
from review_sensei.validation import DEFAULT_REVIEW_LIMITS, ReviewLimits


class _RecordingProvider:
    name = "fixture"
    model: str | None = "fixture-model"
    endpoint = "https://fixture.example/api"
    completion_contract = "bounded-complete-text-v1"

    def __init__(self) -> None:
        self.requests: list[ProviderRequest] = []

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        self.requests.append(request)
        return ProviderResponse(
            text=json.dumps({"summary": "Reviewed cleanly.", "comments": []}),
            provider=self.name,
            model=self.model,
        )


class _LiveTitleMixin:
    live_title: object = "Valid title"

    def external(self, request: Any, timeout: Any) -> Any:
        response = super().external(request, timeout)  # type: ignore[misc]
        if request.full_url.endswith("/pulls/7"):
            pull = json.loads(response.read(524288))
            pull["title"] = self.live_title
            return json_response(pull)
        return response


class _TitleReviewFixture(_LiveTitleMixin, EntryFixture):
    pass


class _TitleCommandFixture(_LiveTitleMixin, CommandEntryFixture):
    pass


class _TitleReplyFixture(_LiveTitleMixin, ReplyEntryFixture):
    pass


class TitleByteLimitsG20Tests(unittest.TestCase):
    def test_routing_preflight_claims_zero_title_validation_credit(self) -> None:
        self.assertEqual(HOSTED_BOOTSTRAP_MAX_TITLE_BYTES, 256)
        self.assertEqual(DIRECT_CORE_MAX_TITLE_BYTES, 4096)
        self.assertEqual(DEFAULT_REVIEW_LIMITS.max_title_bytes, 4096)

        oversized_for_all = "🧪" * 2000  # 8,000 UTF-8 bytes
        routing = evaluate_title_byte_limit(
            oversized_for_all, surface="routing_preflight"
        )
        self.assertFalse(routing.validated)
        self.assertTrue(routing.admitted)
        self.assertIsNone(routing.limit_bytes)
        self.assertFalse(routing.truncated)
        self.assertIn("zero title validation credit", routing.diagnostic)

    def test_utf8_multibyte_boundaries_at_254_255_256_257_and_4095_4096_4097(
        self,
    ) -> None:
        hosted_cases: list[tuple[str, str, int, bool]] = [
            # ASCII (1 byte/char)
            ("ascii-254", "a" * 254, 254, True),
            ("ascii-255", "a" * 255, 255, True),
            ("ascii-256", "a" * 256, 256, True),
            ("ascii-257", "a" * 257, 257, False),
            # 2-byte UTF-8 ('é' = 2 bytes)
            ("latin2b-254", "é" * 127, 254, True),
            ("latin2b-255", ("é" * 127) + "a", 255, True),
            ("latin2b-256", "é" * 128, 256, True),
            ("latin2b-257", ("é" * 128) + "a", 257, False),
            # 3-byte UTF-8 ('漢' = 3 bytes)
            ("cjk3b-254", ("漢" * 84) + "ab", 254, True),
            ("cjk3b-255", "漢" * 85, 255, True),
            ("cjk3b-256", ("漢" * 85) + "a", 256, True),
            ("cjk3b-257", ("漢" * 85) + "é", 257, False),
            # 4-byte UTF-8 ('🧪' = 4 bytes)
            ("emoji4b-254", ("🧪" * 63) + "ab", 254, True),
            ("emoji4b-255", ("🧪" * 63) + "abc", 255, True),
            ("emoji4b-256", "🧪" * 64, 256, True),
            ("emoji4b-257", ("🧪" * 64) + "a", 257, False),
        ]
        for label, sample, expected_bytes, expected_admitted in hosted_cases:
            self.assertEqual(len(sample.encode("utf-8")), expected_bytes)
            for purpose in ("review", "command", "reply"):
                with self.subTest(label=label, purpose=purpose):
                    eval_res = evaluate_title_byte_limit(
                        sample, surface="hosted_bootstrap", hosted_purpose=purpose
                    )
                    self.assertTrue(eval_res.validated)
                    self.assertEqual(eval_res.admitted, expected_admitted)
                    self.assertEqual(eval_res.utf8_bytes, expected_bytes)
                    self.assertEqual(eval_res.codepoint_length, len(sample))
                    self.assertEqual(eval_res.limit_bytes, 256)
                    self.assertFalse(eval_res.truncated)
                    if not expected_admitted:
                        assert eval_res.recovery_instruction is not None
                        self.assertIn(
                            "<= 256 UTF-8 bytes", eval_res.recovery_instruction
                        )

        core_cases: list[tuple[str, str, int, bool]] = [
            ("ascii-4095", "a" * 4095, 4095, True),
            ("ascii-4096", "a" * 4096, 4096, True),
            ("ascii-4097", "a" * 4097, 4097, False),
            ("latin2b-4095", ("é" * 2047) + "a", 4095, True),
            ("latin2b-4096", "é" * 2048, 4096, True),
            ("latin2b-4097", ("é" * 2048) + "a", 4097, False),
            ("cjk3b-4095", "漢" * 1365, 4095, True),
            ("cjk3b-4096", ("漢" * 1365) + "a", 4096, True),
            ("cjk3b-4097", ("漢" * 1365) + "é", 4097, False),
            ("emoji4b-4095", ("🧪" * 1023) + "abc", 4095, True),
            ("emoji4b-4096", "🧪" * 1024, 4096, True),
            ("emoji4b-4097", ("🧪" * 1024) + "a", 4097, False),
        ]
        diff = (
            "diff --git a/src/app.py b/src/app.py\n"
            "--- a/src/app.py\n"
            "+++ b/src/app.py\n"
            "@@ -1 +1,2 @@\n"
            " x = 1\n"
            "+y = 2\n"
        )
        provider = _RecordingProvider()
        service = ReviewService(
            provider,
            stages=(
                Stage(
                    "review",
                    "Context:\n{context_text}\nDiff:\n{diff}",
                    ("summary", "comments"),
                ),
            ),
        )
        for label, sample, expected_bytes, expected_admitted in core_cases:
            self.assertEqual(len(sample.encode("utf-8")), expected_bytes)
            with self.subTest(label=label):
                core_eval = evaluate_title_byte_limit(sample, surface="direct_core")
                self.assertEqual(core_eval.admitted, expected_admitted)
                self.assertEqual(core_eval.utf8_bytes, expected_bytes)
                self.assertEqual(core_eval.limit_bytes, 4096)
                if expected_admitted:
                    req = ReviewRequest(
                        diff=diff,
                        repository="owner/repo",
                        pull_request_number=7,
                        title=sample,
                        base_sha=BASE,
                        head_sha=HEAD,
                    )
                    self.assertEqual(req.title, sample)
                else:
                    calls_before = len(provider.requests)
                    with self.assertRaisesRegex(
                        ReviewInputError, "title exceeds the configured size limit"
                    ):
                        ReviewRequest(diff=diff, title=sample)
                    self.assertEqual(len(provider.requests), calls_before)

        # Direct core recovery once shortened from 4,097 bytes to 4,096 bytes:
        recovered_title = "🧪" * 1024
        run = service.run(
            ReviewRequest(
                diff=diff,
                repository="owner/repo",
                pull_request_number=7,
                title=recovered_title,
                base_sha=BASE,
                head_sha=HEAD,
            )
        )
        assert run.result is not None
        self.assertEqual(run.result.review_status, "complete")
        self.assertIn(recovered_title, provider.requests[-1].prompt)

    def test_hosted_review_entry_refuses_above_256_utf8_bytes_and_recovers_when_shortened(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = _TitleReviewFixture(directory)
            # 65 characters, 257 UTF-8 bytes -> must fail closed by bytes, NOT char count!
            fixture.live_title = ("🧪" * 64) + "a"
            self.assertEqual(len(str(fixture.live_title)), 65)
            self.assertEqual(len(str(fixture.live_title).encode("utf-8")), 257)

            self.assertEqual(fixture.invoke(), 1)
            self.assertIn(
                "live pull request title exceeds its 256-byte bound", fixture.error
            )
            self.assertEqual(fixture.provider_calls, 0)
            self.assertIsNone(fixture.comment)
            self.assertFalse(
                any("/api/github/token" in path for _, _, path in fixture.trace)
            )

            # Non-string title in upstream payload also refuses before enrollment:
            fixture.run_id += 1
            fixture.trace.clear()
            fixture.transports.clear()
            fixture.live_title = 99999
            self.assertEqual(fixture.invoke(), 1)
            self.assertEqual(fixture.provider_calls, 0)
            self.assertIsNone(fixture.comment)

            # Immediate clean recovery once PR title is shortened to <= 256 UTF-8 bytes:
            fixture.run_id += 1
            fixture.trace.clear()
            fixture.transports.clear()
            exact_256 = "🧪" * 64
            fixture.live_title = exact_256
            self.assertEqual(fixture.invoke(), 0, fixture.error)
            self.assertEqual(fixture.selected.title, exact_256)
            self.assertEqual(fixture.provider_calls, 1)
            record = parse_session_comment(fixture.comment["body"], identity=IDENTITY)
            assert record is not None and record.transaction is not None
            self.assertEqual(record.transaction.phase, "publication_succeeded")

    def test_hosted_command_entry_refuses_above_256_utf8_bytes_and_recovers_when_shortened(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = _TitleCommandFixture(directory)
            fixture.live_title = "Initial valid title"
            self.assertEqual(fixture.invoke(), 0, fixture.error)
            baseline_comment_body = fixture.comment["body"]
            baseline_record = parse_session_comment(
                baseline_comment_body, identity=IDENTITY
            )
            assert baseline_record is not None

            # Edit PR title to 257 UTF-8 bytes (86 CJK/accented chars) before maintainer command:
            fixture.select_command("@reviewsensei review pause", new_run=True)
            fixture.live_title = ("漢" * 85) + "é"
            self.assertEqual(len(str(fixture.live_title).encode("utf-8")), 257)

            self.assertEqual(fixture.invoke_command(), 1)
            self.assertIn(
                "live pull request title exceeds its 256-byte bound", fixture.error
            )
            self.assertEqual(fixture.business_writes, 0)
            self.assertEqual(fixture.broker_requests, [])
            self.assertEqual(fixture.comment["body"], baseline_comment_body)

            # Shorten PR title to exact 256 UTF-8 bytes -> command executes & settles cleanly:
            fixture.select_command("@reviewsensei review pause", new_run=True)
            fixture.live_title = ("漢" * 85) + "a"
            self.assertEqual(len(str(fixture.live_title).encode("utf-8")), 256)

            self.assertEqual(fixture.invoke_command(), 0, fixture.error)
            updated_record = parse_session_comment(
                fixture.comment["body"], identity=IDENTITY
            )
            assert updated_record is not None
            self.assertTrue(updated_record.operator_paused)
            self.assertGreater(updated_record.generation, baseline_record.generation)
            assert updated_record.api_run_accounting is not None
            action_state = updated_record.api_run_accounting["action"]
            assert isinstance(action_state, dict)
            self.assertEqual(action_state["phase"], "terminal")

    def test_hosted_reply_entry_refuses_above_256_utf8_bytes_and_recovers_when_shortened(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = _TitleReplyFixture(directory)
            # 129 characters, 257 UTF-8 bytes -> refused before reply inference or session write!
            fixture.live_title = ("é" * 128) + "a"
            self.assertEqual(len(str(fixture.live_title).encode("utf-8")), 257)

            self.assertEqual(fixture.invoke_reply(), 1)
            self.assertIn(
                "live pull request title exceeds its 256-byte bound", fixture.error
            )
            self.assertEqual(len(fixture.provider_requests), 0)
            self.assertEqual(fixture.reply_posts, 0)
            self.assertIsNone(fixture.comment)

            # Shorten PR title to exact 256 UTF-8 bytes -> reply runs and settles cleanly:
            fixture.run_id += 1
            fixture.trace.clear()
            fixture.transports.clear()
            fixture.live_title = "é" * 128
            self.assertEqual(len(str(fixture.live_title).encode("utf-8")), 256)

            self.assertEqual(fixture.invoke_reply(), 0, fixture.error)
            self.assertEqual(len(fixture.provider_requests), 1)
            self.assertEqual(fixture.reply_posts, 1)
            self.assertEqual(fixture.action()["phase"], "terminal")

    def test_invalid_utf8_surrogates_non_string_and_guard_invariants(self) -> None:
        non_str = evaluate_title_byte_limit(None, surface="hosted_bootstrap")
        self.assertFalse(non_str.admitted)
        self.assertTrue(non_str.validated)
        self.assertIsNone(non_str.utf8_bytes)
        self.assertFalse(non_str.to_dict()["truncated"])

        surrogate = evaluate_title_byte_limit(
            "bad-\ud800-title", surface="direct_core", custom_core_limit=512
        )
        self.assertFalse(surrogate.admitted)
        self.assertEqual(surrogate.limit_bytes, 512)
        assert surrogate.recovery_instruction is not None
        self.assertIn("surrogates", surrogate.recovery_instruction)

        narrow_limits = ReviewLimits(max_title_bytes=512)
        with self.assertRaises(ReviewInputError):
            ReviewRequest(
                diff="diff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -1 +1 @@\n-a\n+b\n",
                title="x" * 513,
                limits=narrow_limits,
            )

        for bad_call in (
            lambda: evaluate_title_byte_limit("ok", surface="unknown"),
            lambda: evaluate_title_byte_limit(
                "ok", surface="hosted_bootstrap", hosted_purpose="invalid"
            ),
            lambda: evaluate_title_byte_limit(
                "ok", surface="hosted_bootstrap", custom_core_limit=128
            ),
            lambda: evaluate_title_byte_limit(
                "ok", surface="routing_preflight", hosted_purpose="review"
            ),
            lambda: evaluate_title_byte_limit(
                "ok", surface="direct_core", hosted_purpose="review"
            ),
            lambda: evaluate_title_byte_limit(
                "ok", surface="direct_core", custom_core_limit=8192
            ),
        ):
            with self.assertRaises(ReviewInputError):
                bad_call()

        with self.assertRaises(ReviewInputError):
            TitleByteLimitEvaluation(
                surface="hosted_bootstrap",
                hosted_purpose="review",
                validated=True,
                admitted=True,
                utf8_bytes=10,
                codepoint_length=10,
                limit_bytes=256,
                truncated=True,
                recovery_instruction=None,
                diagnostic="truncated title is forbidden",
            )
        with self.assertRaises(ReviewInputError):
            TitleByteLimitEvaluation(
                surface="routing_preflight",
                hosted_purpose=None,
                validated=True,
                admitted=True,
                utf8_bytes=None,
                codepoint_length=None,
                limit_bytes=None,
                truncated=False,
                recovery_instruction=None,
                diagnostic="routing preflight cannot claim validation",
            )


if __name__ == "__main__":
    unittest.main()
