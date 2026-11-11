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
from review_sensei.models import (
    ConversationContext,
    ProviderRequest,
    ProviderResponse,
    ReviewRequest,
)
from review_sensei.operational_boundaries import (
    CONVERSATION_CONTEXT_MAX_TITLE_BYTES,
    CONVERSATION_PUBLISHER_TITLE_CAP_BYTES,
    DIRECT_CORE_MAX_TITLE_BYTES,
    HOSTED_BOOTSTRAP_MAX_TITLE_BYTES,
    TitleByteLimitEvaluation,
    TitleConstantContract,
    evaluate_title_byte_limit,
    inspect_title_constant_contracts,
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
            raw = (
                bytes(response.body)
                if hasattr(response, "body")
                else bytes(response.read())
            )
            pull = dict(json.loads(raw.decode("utf-8")))
            pull["title"] = self.live_title
            return json_response(pull, status=getattr(response, "status", 200))
        return response


def _reset_hosted_run(fixture: Any, *, live_title: object) -> None:
    """Atomically advance run_id, clear transport state, and bind live_title."""
    fixture.live_title = live_title
    fixture.run_id += 1
    fixture.trace.clear()
    fixture.transports.clear()


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
                core_limits = ReviewLimits(max_title_bytes=DIRECT_CORE_MAX_TITLE_BYTES)
                common_req_kwargs: dict[str, Any] = {
                    "diff": diff,
                    "repository": "owner/repo",
                    "pull_request_number": 7,
                    "title": sample,
                    "base_sha": BASE,
                    "head_sha": HEAD,
                    "limits": core_limits,
                }
                if expected_admitted:
                    req = ReviewRequest(**common_req_kwargs)
                    self.assertEqual(req.title, sample)
                else:
                    calls_before = len(provider.requests)
                    with self.assertRaisesRegex(
                        ReviewInputError,
                        "review title exceeds the configured size limit",
                    ):
                        ReviewRequest(**common_req_kwargs)
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
            _reset_hosted_run(fixture, live_title=99999)
            self.assertEqual(fixture.invoke(), 1)
            self.assertEqual(fixture.provider_calls, 0)
            self.assertIsNone(fixture.comment)

            # Immediate clean recovery once PR title is shortened to <= 256 UTF-8 bytes:
            exact_256 = "🧪" * 64
            _reset_hosted_run(fixture, live_title=exact_256)
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
            _reset_hosted_run(fixture, live_title="é" * 128)
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

    def test_real_entry_point_utf8_limit_minus_1_exact_limit_plus_1_and_multibyte_straddles(
        self,
    ) -> None:
        """Exercise real hosted (review/command/reply) and core CLI/ConversationContext entry points at exact byte boundaries."""
        import io
        from contextlib import redirect_stderr, redirect_stdout
        from pathlib import Path
        from unittest.mock import patch

        import test_review_transaction as tx
        from review_sensei_github.conversation import MAX_CONTEXT_PR_TITLE_BYTES

        from review_sensei import cli as core_cli

        # 1. Verify source-level constants across all surfaces match live runtime symbols.
        contracts = {c.surface_id: c for c in inspect_title_constant_contracts()}
        self.assertEqual(len(contracts), 5)
        self.assertIsNone(contracts["routing_preflight"].limit_bytes)
        self.assertEqual(
            contracts["hosted_bootstrap_review_command_reply"].limit_bytes, 256
        )
        self.assertEqual(
            contracts["hosted_conversation_publisher"].limit_bytes,
            MAX_CONTEXT_PR_TITLE_BYTES,
        )
        self.assertEqual(CONVERSATION_PUBLISHER_TITLE_CAP_BYTES, 2048)
        self.assertEqual(
            contracts["direct_core_review_request_and_cli"].limit_bytes, 4096
        )
        self.assertEqual(
            contracts["direct_core_conversation_context"].limit_bytes,
            CONVERSATION_CONTEXT_MAX_TITLE_BYTES,
        )
        with self.assertRaises(ReviewInputError):
            TitleConstantContract(
                surface_id="forged",
                limit_bytes=256,
                enforces_utf8_bytes=True,
                truncates_silently=True,
                source_symbol="forged",
            )
        with self.assertRaises(ReviewInputError):
            TitleConstantContract(
                surface_id="forged_mismatch",
                limit_bytes=None,
                enforces_utf8_bytes=True,
                truncates_silently=False,
                source_symbol="forged",
            )

        # 2. Real hosted review-run, command-run, and reply-run at 255 (limit-1), 256 (exact),
        #    257 (limit+1), and 2/3/4-byte codepoints straddling the 256-byte boundary.
        hosted_boundary_matrix: list[tuple[str, str, int, bool]] = [
            ("limit-minus-1-multibyte-255", ("a" * 251) + "🧪", 255, True),
            ("exact-limit-multibyte-256", ("a" * 252) + "🧪", 256, True),
            ("limit-plus-1-ascii-257", "a" * 257, 257, False),
            ("straddle-2b-255-to-257", ("a" * 255) + "é", 257, False),
            ("straddle-3b-255-to-258", ("a" * 255) + "漢", 258, False),
            ("straddle-4b-255-to-259", ("a" * 255) + "🧪", 259, False),
        ]
        for label, title_sample, byte_count, should_admit in hosted_boundary_matrix:
            self.assertEqual(len(title_sample.encode("utf-8")), byte_count)
            with self.subTest(surface="review-run", label=label):
                with tempfile.TemporaryDirectory() as rev_dir:
                    rev_f = _TitleReviewFixture(rev_dir)
                    rev_f.live_title = title_sample
                    rc = rev_f.invoke()
                    if should_admit:
                        self.assertEqual(rc, 0, rev_f.error)
                        self.assertEqual(rev_f.selected.title, title_sample)
                        self.assertEqual(rev_f.provider_calls, 1)
                    else:
                        self.assertEqual(rc, 1)
                        self.assertIn("256-byte bound", rev_f.error)
                        self.assertEqual(rev_f.provider_calls, 0)
                        self.assertIsNone(rev_f.comment)

            with self.subTest(surface="reply-run", label=label):
                with tempfile.TemporaryDirectory() as rep_dir:
                    rep_f = _TitleReplyFixture(rep_dir)
                    rep_f.live_title = title_sample
                    rc = rep_f.invoke_reply()
                    if should_admit:
                        self.assertEqual(rc, 0, rep_f.error)
                        self.assertEqual(len(rep_f.provider_requests), 1)
                        self.assertIn(title_sample, rep_f.provider_requests[0].prompt)
                        self.assertEqual(rep_f.reply_posts, 1)
                    else:
                        self.assertEqual(rc, 1)
                        self.assertIn("256-byte bound", rep_f.error)
                        self.assertEqual(len(rep_f.provider_requests), 0)
                        self.assertEqual(rep_f.reply_posts, 0)

        with tempfile.TemporaryDirectory() as cmd_dir:
            cmd_f = _TitleCommandFixture(cmd_dir)
            cmd_f.live_title = "Seed title"
            self.assertEqual(cmd_f.invoke(), 0, cmd_f.error)
            toggle_commands = [
                "@reviewsensei review pause",
                "@reviewsensei review continue",
            ]
            admitted_idx = 0
            for (
                label,
                title_sample,
                _byte_count,
                should_admit,
            ) in hosted_boundary_matrix:
                with self.subTest(surface="command-run", label=label):
                    prior_body = cmd_f.comment["body"]
                    cmd_text = toggle_commands[admitted_idx % 2]
                    cmd_f.select_command(cmd_text, new_run=True)
                    cmd_f.live_title = title_sample
                    rc = cmd_f.invoke_command()
                    if should_admit:
                        self.assertEqual(rc, 0, cmd_f.error)
                        admitted_idx += 1
                    else:
                        self.assertEqual(rc, 1)
                        self.assertIn("256-byte bound", cmd_f.error)
                        self.assertEqual(cmd_f.comment["body"], prior_body)

        # 3. Real core CLI (`review_sensei.cli.main`) & ConversationContext at 4095, 4096, 4097+
        #    including 2/3/4-byte UTF-8 boundary straddles, with zero silent truncation.
        core_entry_matrix: list[tuple[str, str, int, bool]] = [
            ("core-limit-minus-1-4095", ("🧪" * 1023) + "abc", 4095, True),
            ("core-exact-limit-4096", ("漢" * 1364) + "🧪", 4096, True),
            ("core-limit-plus-1-ascii-4097", "a" * 4097, 4097, False),
            ("core-straddle-2b-4095-to-4097", ("a" * 4095) + "é", 4097, False),
            ("core-straddle-3b-4095-to-4098", ("a" * 4095) + "漢", 4098, False),
            ("core-straddle-4b-4095-to-4099", ("a" * 4095) + "🧪", 4099, False),
        ]
        with tempfile.TemporaryDirectory() as cli_dir:
            diff_path = Path(cli_dir) / "change.patch"
            diff_path.write_text(
                "diff --git a/src/app.py b/src/app.py\n"
                "--- a/src/app.py\n"
                "+++ b/src/app.py\n"
                "@@ -1 +1,2 @@\n"
                " x = 1\n"
                "+y = 2\n",
                encoding="utf-8",
            )
            for label, sample, byte_count, should_admit in core_entry_matrix:
                self.assertEqual(len(sample.encode("utf-8")), byte_count)
                with self.subTest(surface="core-cli-and-conversation", label=label):
                    if should_admit:
                        ctx = ConversationContext(
                            pull_request_number=7,
                            base_sha=BASE,
                            head_sha=HEAD,
                            pull_request_title=sample,
                        )
                        self.assertEqual(ctx.pull_request_title, sample)
                    else:
                        with self.assertRaisesRegex(
                            ReviewInputError,
                            "conversation pull request title exceeds the configured size limit",
                        ):
                            ConversationContext(
                                pull_request_number=7,
                                base_sha=BASE,
                                head_sha=HEAD,
                                pull_request_title=sample,
                            )

                    cli_provider = _RecordingProvider()
                    err_buf = io.StringIO()
                    with (
                        patch(
                            "review_sensei.cli.default_registry",
                            return_value=tx.HostedAnalysisCliTests._Registry(
                                cli_provider
                            ),
                        ),
                        redirect_stdout(io.StringIO()),
                        redirect_stderr(err_buf),
                    ):
                        rc = core_cli.main(
                            [
                                "--diff",
                                str(diff_path),
                                "--repository",
                                "owner/repo",
                                "--pull-request",
                                "7",
                                "--base-sha",
                                BASE,
                                "--head-sha",
                                HEAD,
                                "--no-learning-proposals",
                                f"--title={sample}",
                            ]
                        )
                    if should_admit:
                        self.assertEqual(rc, 0, err_buf.getvalue())
                        self.assertEqual(len(cli_provider.requests), 1)
                        self.assertIn(sample, cli_provider.requests[0].prompt)
                    else:
                        self.assertNotEqual(rc, 0)
                        self.assertEqual(len(cli_provider.requests), 0)
                        self.assertIn(
                            "review title exceeds the configured size limit",
                            err_buf.getvalue(),
                        )


if __name__ == "__main__":
    unittest.main()
