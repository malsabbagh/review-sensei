"""One public hosted analysis path with real ledger and caller enforcement."""

from __future__ import annotations

import copy
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from review_sensei_github.api_run_host import HOSTED_API_STARTUP_PLAN, ApiRunReviewHooks
from review_sensei_github.api_run_requests import (
    NATIVE_REPLACE_CALLER_REQUESTS,
    ApiRunRequestTransport,
    ApiRunSessionLedger,
)
from review_sensei_github.errors import GitHubPublicationTransientError
from review_sensei_github.session_ledger import parse_session_comment

from review_sensei.api_work import ApiRunAccounting
from review_sensei.bounded_evidence import EvidenceReadBudget
from review_sensei.cli import main
from review_sensei.errors import ReviewInputError
from tests import test_review_transaction as tx
from tests.fake_github_http import json_response
from tests.test_api_work_persistence import BASE, HEAD, IDENTITY, StartupFixture


class HostedRunFixture(StartupFixture):
    def __init__(self, directory):
        super().__init__(directory)
        self.extra_comments = {}

    def _external_open(self, request, timeout):
        target = urlsplit(request.full_url)
        path, method = target.path, request.method
        payload = json.loads(request.data) if request.data else None
        listing = path.endswith("/issues/7/comments")
        identifier = (
            int(path.rsplit("/", 1)[1]) if "/issues/comments/" in path else None
        )
        extra_write = (
            (listing or identifier is not None)
            and payload
            and "reviewsensei:session:" not in payload.get("body", "")
        )
        user = path == "/user" or path.startswith("/users/")
        if (
            listing
            and method == "GET"
            or extra_write
            or identifier in self.extra_comments
            or user
        ):
            assert (
                len(self.accounted.observations)
                == sum(a == self.attempt for a, _, _ in self.trace) + 1
            )
            self.trace.append((self.attempt, method, path))
            bot = {"id": 999, "login": "reviewsensei[bot]", "type": "Bot"}
            if user:
                return json_response(bot)
            if listing and method == "GET":
                query = parse_qs(target.query)
                size, page = (
                    int(query.get("per_page", [100])[0]),
                    int(query.get("page", [1])[0]),
                )
                rows = ([self.comment] if self.comment else []) + list(
                    self.extra_comments.values()
                )
                return json_response(
                    copy.deepcopy(rows[(page - 1) * size : page * size])
                )
            if method in {"POST", "PATCH"}:
                identifier = identifier or 2_000_000 + len(self.extra_comments)
                item = {
                    "id": identifier,
                    "body": payload["body"],
                    "user": bot,
                    "issue_url": "https://api.github.test/repos/owner/repo/issues/7",
                }
                self.extra_comments[identifier] = item
                return json_response(
                    copy.deepcopy(item), 201 if method == "POST" else 200
                )
            return json_response(copy.deepcopy(self.extra_comments[identifier]))
        response = super()._external_open(request, timeout)
        if self.comment is not None:
            self.comment["issue_url"] = (
                "https://api.github.test/repos/owner/repo/issues/7"
            )
        if path.endswith("/pulls/7"):
            pr = json.loads(response.read(524288))
            pr.update(state="open", draft=False)
            pr["base"]["ref"] = "main"
            return json_response(pr)
        return response


class PublicCallerRunTests(unittest.TestCase):
    def test_real_hosted_command_never_constructs_a_phase_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = HostedRunFixture(directory)
            native, config, _evidence, owner = fixture.prelude()
            self.assertEqual(fixture.accounted.plan, HOSTED_API_STARTUP_PLAN)
            fixture.accounted.initialize_and_activate(native, IDENTITY, config, owner)
            ledger = ApiRunSessionLedger(native, fixture.accounted, IDENTITY)
            args = tx.HostedAnalysisCliTests()._argv(
                Path(directory),
                admission_context_output=Path(directory) / "admission.json",
            )
            index = args.index("--session-ledger")
            del args[index : index + 2]
            args[args.index("--pull-request") + 1] = str(IDENTITY.pull_request)
            args.extend(
                [
                    "--github-session-ledger",
                    "--repository-id",
                    str(IDENTITY.repository_id),
                ]
            )
            args[args.index("--base-sha") + 1] = BASE
            args[args.index("--head-sha") + 1] = HEAD
            config_path = Path(directory) / "trusted.yml"
            config_path.write_bytes(fixture.config_bytes)
            args.extend(["--config", str(config_path)])
            provider_calls = []

            class Provider(tx.HostedAnalysisCliTests._Provider):
                def complete(self, request):
                    provider_calls.append(True)
                    return super().complete(request)

            errors = io.StringIO()
            blocked = []
            native_replace = ApiRunRequestTransport.replace_checkpoint

            def observe(caller, native, identity, mutate=None, **kwargs):
                before = len(fixture.trace)
                try:
                    return native_replace(caller, native, identity, mutate, **kwargs)
                except ReviewInputError as error:
                    if "operation is unfunded" in str(error):
                        blocked.append(
                            {
                                "operation": getattr(
                                    mutate, "__qualname__", "checkpoint"
                                ),
                                "required": NATIVE_REPLACE_CALLER_REQUESTS,
                                "available": caller.usable,
                                "new_http_requests": len(fixture.trace) - before,
                            }
                        )
                    raise

            hooks = ApiRunReviewHooks(ledger, IDENTITY, base_sha=BASE, head_sha=HEAD)
            with (
                patch.object(ApiRunRequestTransport, "replace_checkpoint", observe),
                patch.dict(
                    os.environ,
                    {
                        "GITHUB_RUN_ID": "456",
                        "GITHUB_RUN_ATTEMPT": "1",
                        "REVIEWSENSEI_APP_ID": "999",
                    },
                ),
                patch.object(
                    EvidenceReadBudget,
                    "__init__",
                    side_effect=AssertionError("no phase allowance factory"),
                ),
                patch(
                    "review_sensei.cli.default_registry",
                    return_value=tx.HostedAnalysisCliTests._Registry(Provider()),
                ),
                redirect_stdout(io.StringIO()),
                redirect_stderr(errors),
            ):
                result = main(args, hosted_hooks=hooks)
            self.measurement = {
                "exit_code": result,
                "diagnostic": errors.getvalue(),
                "caller_requests": len(fixture.trace),
                "startup_requests": fixture.accounted.startup_dispatches,
                "ordinary_confirmed": fixture.accounted.accounting.confirmed,
                "ordinary_uncertain": fixture.accounted.accounting.uncertain,
                "ordinary_usable": fixture.accounted.usable,
                "unallocated": fixture.accounted.accounting.remaining,
                "startup_liability": HOSTED_API_STARTUP_PLAN.liability,
                "ordinary_tranche": 128 - HOSTED_API_STARTUP_PLAN.liability,
                "paths": fixture.trace,
                "metadata_comments": len(fixture.extra_comments),
                "provider_calls": len(provider_calls),
                "provider_started": hooks._provider_started,
                "completion_started": hooks._completion_started,
                "blocked_operations": blocked,
                "retained_transaction_phase": parse_session_comment(
                    fixture.comment["body"], identity=IDENTITY
                ).transaction.phase,
                "publication_or_approval_requests": sum(
                    path.endswith("/reviews") for _, _, path in fixture.trace
                ),
            }
            self.assertEqual(result, 2, errors.getvalue())
            self.assertIn("caller checkpoint operation is unfunded", errors.getvalue())
            self.assertEqual(len(provider_calls), 1)
            self.assertEqual(len(blocked), 3)
            self.assertTrue(all(item["new_http_requests"] == 0 for item in blocked))
            self.assertEqual(blocked[0]["available"], 30)
            self.assertEqual(blocked[0]["new_http_requests"], 0)
            self.assertEqual(fixture.accounted.accounting.confirmed, 24)
            self.assertEqual(fixture.accounted.usable, 26)
            self.assertEqual(fixture.accounted.accounting.remaining, 0)
            self.assertEqual(len(fixture.trace), 34)
            self.assertEqual(self.measurement["publication_or_approval_requests"], 0)

    def test_lost_first_run_record_never_transfers_its_reserved_tranche(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = HostedRunFixture(directory)
            native, configuration, _evidence, owner = fixture.prelude()
            fixture.fault = "unknown-stored"
            with self.assertRaises(GitHubPublicationTransientError):
                fixture.accounted.initialize_and_activate(
                    native, IDENTITY, configuration, owner
                )
            record = parse_session_comment(fixture.comment["body"], identity=IDENTITY)
            original = ApiRunAccounting.from_document(record.api_run_accounting)
            self.assertEqual(original.blocks[0].undispatched, 50)
            self.assertEqual(fixture.accounted.usable, 0)
            before = len(fixture.trace)
            with self.assertRaisesRegex(ReviewInputError, "exceeds"):
                fixture.recover(reserve_successor=True)
            self.assertEqual(fixture.accounted.usable, 0)
            retained = ApiRunAccounting.from_document(
                parse_session_comment(
                    fixture.comment["body"], identity=IDENTITY
                ).api_run_accounting
            )
            self.assertEqual(retained, original)
            self.assertFalse(
                any(
                    a == 2 and m in {"POST", "PATCH"} and "/issues" in path
                    for a, m, path in fixture.trace
                )
            )
            self.recovery_measurement = {
                "initial_requests": before,
                "recovery_requests": len(fixture.trace) - before,
                "startup_liability": HOSTED_API_STARTUP_PLAN.liability,
                "predecessor_tranche": 50,
                "unallocated": retained.remaining,
                "successor_usable": fixture.accounted.usable,
            }

    def test_comment_write_keeps_its_bounded_confirmation_tail(self):
        from urllib.request import Request

        for remaining in (10, 11):
            with (
                self.subTest(remaining=remaining),
                tempfile.TemporaryDirectory() as directory,
            ):
                fixture = HostedRunFixture(directory)
                native, configuration, _evidence, owner = fixture.prelude()
                fixture.accounted.initialize_and_activate(
                    native, IDENTITY, configuration, owner
                )
                for _ in range(50 - remaining):
                    fixture.http.request(
                        "GET", "/repos/owner/repo/pulls/7", token="synthetic"
                    )
                before = len(fixture.trace)
                request = Request(
                    "https://api.github.test/repos/owner/repo/issues/7/comments",
                    data=b'{"body":"synthetic carrier"}',
                    method="POST",
                )
                if remaining == 10:
                    with self.assertRaisesRegex(
                        ReviewInputError, "comment write/readback tail"
                    ):
                        fixture.accounted.open(request, timeout=30)
                    self.assertEqual(len(fixture.trace), before)
                else:
                    with fixture.accounted.open(request, timeout=30) as response:
                        response.read(524288)
                    self.assertEqual(len(fixture.trace), before + 1)
                    self.assertEqual(fixture.accounted.usable, 10)
