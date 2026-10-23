"""An explicit remaining-work event buys one scoped cumulative window."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from review_sensei_github import trigger
from review_sensei_github.publication import (
    approval_eligibility_from_result,
    approval_eligibility_marker,
    review_marker,
)
from review_sensei_github.rescan import cli_event_review_intent, resolve_rescan

from review_sensei.cli import main
from review_sensei.errors import ReviewInputError
from review_sensei.models import ProviderResponse
from review_sensei.rescan import AuthorizedRescan, replayed_rescan, rescan_reservation
from review_sensei.session import InMemorySessionLedger, complete_review_publication
from review_sensei.validation import ReviewLimits
from tests import test_explicit_rescan as rescan
from tests import test_review_transaction as tx


class OriginalEventIntentTests(unittest.TestCase):
    def test_valid_event_is_read_once_and_returns_only_closed_intent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "event.json"
            for body, expected in (
                ("@reviewsensei review remaining\n", "--review-remaining"),
                ("@reviewsensei rescan", "--rescan"),
                ("ordinary discussion", ""),
            ):
                event = json.dumps({"comment": {"body": body}})
                path.write_text(event, encoding="utf-8")
                with (
                    self.subTest(body=body),
                    patch.dict(
                        os.environ,
                        {
                            "GITHUB_EVENT_NAME": "issue_comment",
                            "GITHUB_EVENT_PATH": str(path),
                        },
                    ),
                    patch.object(Path, "read_text", return_value=event) as read,
                ):
                    self.assertEqual(cli_event_review_intent(), expected)
                    self.assertEqual(read.call_count, 1)

    def test_unavailable_or_invalid_event_refuses_instead_of_ordinary_review(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "event.json"
            with patch.dict(
                os.environ,
                {"GITHUB_EVENT_NAME": "issue_comment", "GITHUB_EVENT_PATH": str(path)},
            ):
                for content in (None, b"{", b"[]", b"\xff", b" " * (1024 * 1024 + 1)):
                    with self.subTest(
                        content_size=None if content is None else len(content)
                    ):
                        if content is not None:
                            path.write_bytes(content)
                        with self.assertRaisesRegex(
                            ReviewInputError, "original review intent event"
                        ):
                            cli_event_review_intent()
                path.write_text("{}", encoding="utf-8")
                with patch.object(
                    Path, "read_text", side_effect=OSError("synthetic read failure")
                ):
                    with self.assertRaisesRegex(
                        ReviewInputError, "original review intent event"
                    ):
                        cli_event_review_intent()

    def test_unexpected_predicate_failure_propagates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "event.json"
            path.write_text("{}", encoding="utf-8")
            with (
                patch.dict(
                    os.environ,
                    {
                        "GITHUB_EVENT_NAME": "issue_comment",
                        "GITHUB_EVENT_PATH": str(path),
                    },
                ),
                patch(
                    "review_sensei_github.rescan.event_requests_remaining",
                    side_effect=RuntimeError("synthetic internal failure"),
                ),
                self.assertRaises(RuntimeError),
            ):
                cli_event_review_intent()


class RemainingReviewAdmissionTests(unittest.TestCase):
    def test_standalone_command_routes_to_review_without_changing_rescan_or_continue(
        self,
    ):
        pull = {
            "number": 146,
            "head": {"sha": tx.HEAD_SHA, "ref": "feature"},
            "base": {"sha": tx.BASE_SHA, "ref": "main"},
        }
        for body in (
            "@reviewsensei review remaining",
            "@ReviewSensei REVIEW remaining commit " + tx.HEAD_SHA[:9],
            "@sensei review remaining\t",
        ):
            with self.subTest(body=body):
                self.assertTrue(trigger.issue_comment_requests_remaining(body))
                self.assertFalse(trigger.issue_comment_requests_rescan(body))
                self.assertEqual(
                    trigger.resolve_issue_comment(body, pull).operation, "review"
                )
        for body in (
            "> @reviewsensei review remaining",
            "please @reviewsensei review remaining",
            "@reviewsensei review remaining\nmore",
            "@reviewsensei review remaining Ａ",
            "@reviewsensei review remaining commit " + "a" * 41,
        ):
            self.assertFalse(trigger.issue_comment_requests_remaining(body))
        self.assertEqual(
            trigger.resolve_issue_comment(
                "@reviewsensei review continue", pull
            ).operation,
            "command",
        )
        self.assertTrue(trigger.issue_comment_requests_rescan("@reviewsensei rescan"))

    def test_remaining_mode_has_distinct_reservation_but_shared_replay_watermark(self):
        ledger, old = rescan.seed()
        record = ledger.load(tx.IDENTITY).record
        full = rescan.request(ledger, 10, "issue_comment")
        remaining = replace(full, mode="remaining")
        self.assertNotEqual(
            rescan_reservation(record, full, tx.HEAD_SHA),
            rescan_reservation(record, remaining, tx.HEAD_SHA),
        )
        prepared = rescan.prepare(ledger, remaining)
        self.assertTrue(prepared.decision.admit)
        stored = ledger.load(tx.IDENTITY).record
        self.assertEqual(stored.rescan_state["receipts"][-1]["mode"], "remaining")
        self.assertTrue(replayed_rescan(stored, remaining))
        self.assertTrue(replayed_rescan(stored, full))
        self.assertEqual(
            stored.rescan_state["receipts"][-1]["source"]["transaction_id"],
            old.transaction.transaction_id,
        )
        with self.assertRaises(ReviewInputError):
            AuthorizedRescan("issue_comment", 12, full.source, 5, mode="anything")


class RemainingSourceAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = rescan.RescanHostTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.ledger, bound = rescan.seed(
            result=replace(
                rescan.partial.partial_result(
                    "budget-exhausted", "provider-call-budget"
                ),
                continuation_scope_digest="0" * 64,
            )
        )
        self.fixture.ledger = self.ledger
        self.fixture.http = rescan.AuthorityHTTP(bound)
        self.ledger.http = self.fixture.http
        self.ledger.token = "synthetic-token"
        self.args = self.fixture.args
        self.args.review_remaining = True
        self.fixture.event["inputs"]["review_remaining"] = "true"
        self.fixture.write_event()
        self.source = self.ledger.load(tx.IDENTITY).record.transaction
        self.parent = self.ledger.load_retained_checkpoint_result(
            tx.IDENTITY, transaction=self.source, head_sha=tx.HEAD_SHA
        )
        self.parent = replace(self.parent, continuation_scope_digest="0" * 64)
        self.ledger.retain_checkpoint_result(self.parent)
        eligibility = approval_eligibility_from_result(
            self.parent, head_sha=tx.HEAD_SHA, enabled=True, app_authored=False
        )
        self.fixture.http.review["body"] = (
            review_marker(
                repository_id=99,
                pull_request=146,
                head_sha=tx.HEAD_SHA,
                result=self.parent,
            )
            + "\n"
            + approval_eligibility_marker(eligibility)
        )
        original = self.fixture.http.request

        def request(method, path, **kwargs):
            status, payload = original(method, path, **kwargs)
            if path.endswith("/pulls/146"):
                payload["base"]["sha"] = tx.BASE_SHA
            return status, payload

        self.fixture.http.request = request

    def test_remaining_requires_exact_published_result_not_just_same_head(self):
        authority = resolve_rescan(self.args, self.ledger, actions_token="synthetic")
        self.assertEqual(authority.mode, "remaining")
        self.assertEqual(authority.source.transaction_id, self.source.transaction_id)
        changed = replace(
            self.parent, summary="Different same-head result", transaction=None
        )
        eligibility = approval_eligibility_from_result(
            changed, head_sha=tx.HEAD_SHA, enabled=True, app_authored=False
        )
        self.fixture.http.review["body"] = (
            review_marker(
                repository_id=99, pull_request=146, head_sha=tx.HEAD_SHA, result=changed
            )
            + "\n"
            + approval_eligibility_marker(eligibility)
        )
        with self.assertRaisesRegex(
            ReviewInputError, "remaining.*source|source.*remaining"
        ):
            resolve_rescan(self.args, self.ledger, actions_token="synthetic")

    def test_missing_retained_parent_refuses_without_full_analysis_fallback(self):
        with patch.object(
            self.ledger, "load_retained_checkpoint_result", return_value=None
        ):
            with self.assertRaisesRegex(
                ReviewInputError, "remaining.*retained|retained.*remaining"
            ):
                resolve_rescan(self.args, self.ledger, actions_token="synthetic")


class RemainingCLIJourneyTests(unittest.TestCase):
    def test_actual_cli_new_event_reviews_only_remaining_file_and_duplicate_is_free(
        self,
    ):
        fixture = rescan.RescanHostTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        ledger = InMemorySessionLedger()
        ledger.http, ledger.token = fixture.http, "synthetic"
        root = Path(fixture.temp.name)
        diff = "".join(
            f"diff --git a/src/mod_{i}.py b/src/mod_{i}.py\nnew file mode 100644\nindex 0000000..1111111\n--- /dev/null\n+++ b/src/mod_{i}.py\n@@ -0,0 +1,2 @@\n+value_{i}=1\n+other_{i}=2\n"
            for i in range(9)
        )
        args = rescan.cli_arguments(root, diff=diff)
        args.remove("--rescan")
        config = root / "config.yml"
        config.write_text(
            "schema: 1\nadvanced:\n  resources:\n    max_provider_calls: 8\n  large_changes:\n    orchestrate: true\n"
        )
        args.extend(["--config", str(config), "--orchestrate-large-changes"])

        class Provider:
            name, model = "fixture", "fixture-model"

            def __init__(self):
                self.calls = []

            def complete(self, request):
                self.calls.append(request.prompt)
                return ProviderResponse(
                    '{"summary":"Synthetic complete chunk.","comments":[]}',
                    self.name,
                    self.model,
                )

        provider = Provider()

        class Registry:
            def create(self, settings):
                return provider

        class Hooks(type(rescan.ledger_hooks(ledger))):
            def retain_checkpoint_result(
                self, args, ledger, identity, result, **kwargs
            ):
                ledger.retain_checkpoint_result(result)

        limits = ReviewLimits(
            max_diff_bytes=65536,
            max_diff_lines=1600,
            max_diff_files=1,
            max_diff_hunks=1,
        )
        errors = io.StringIO()
        with (
            patch("review_sensei.cli.DEFAULT_REVIEW_LIMITS", limits),
            patch("review_sensei.cli.default_registry", return_value=Registry()),
            redirect_stderr(errors),
        ):
            self.assertEqual(main(args, hosted_hooks=Hooks()), 0, errors.getvalue())
        record = ledger.load(tx.IDENTITY).record
        prior = ledger.load_retained_checkpoint_result(
            tx.IDENTITY, transaction=record.transaction, head_sha=tx.HEAD_SHA
        )
        self.assertEqual(prior.review_status, "partial")
        self.assertEqual(
            sum(item.outcome == "reviewed" for item in prior.coverage.files), 8
        )
        complete_review_publication(
            ledger, tx.IDENTITY, record.transaction, published=True
        )
        http = rescan.AuthorityHTTP(prior)
        original_request = http.request

        def request(method, path, **kwargs):
            status, payload = original_request(method, path, **kwargs)
            if path.endswith("/pulls/146"):
                payload["base"]["sha"] = tx.BASE_SHA
            return status, payload

        http.request = request
        eligibility = approval_eligibility_from_result(
            prior, head_sha=tx.HEAD_SHA, enabled=True, app_authored=False
        )
        http.review["body"] = (
            review_marker(
                repository_id=99, pull_request=146, head_sha=tx.HEAD_SHA, result=prior
            )
            + "\n"
            + approval_eligibility_marker(eligibility)
        )
        http.comment.update(id=21, body="@reviewsensei review remaining")
        ledger.http = http
        fixture.event.update(
            action="created", issue={"number": 146}, comment=dict(http.comment)
        )
        fixture.write_event()
        os.environ["GITHUB_EVENT_NAME"] = "issue_comment"
        args.append("--review-remaining")
        with (
            patch("review_sensei.cli.DEFAULT_REVIEW_LIMITS", limits),
            patch("review_sensei.cli.default_registry", return_value=Registry()),
            redirect_stderr(errors),
        ):
            self.assertEqual(main(args, hosted_hooks=Hooks()), 0, errors.getvalue())
            self.assertEqual(len(provider.calls), 9)
            # The same event sees publication_pending and requests supported publication recovery.
            self.assertEqual(main(args, hosted_hooks=Hooks()), 1, errors.getvalue())
            self.assertEqual(len(provider.calls), 9)
        record = ledger.load(tx.IDENTITY).record
        result = ledger.load_retained_checkpoint_result(
            tx.IDENTITY, transaction=record.transaction, head_sha=tx.HEAD_SHA
        )
        self.assertEqual(result.review_status, "complete")
        self.assertTrue(result.coverage.fully_reviewed)
