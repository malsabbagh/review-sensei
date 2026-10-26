"""Closed-plan public CLI/application lifecycle with synthetic external services."""

from __future__ import annotations

import copy
import io
import json
import os
import tempfile
import unittest
from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from review_sensei_github import GitHubApplication, GitHubWriteOptions, ReviewPublisher
from review_sensei_github.api_run_host import HOSTED_API_STARTUP_PLAN, ApiRunReviewHooks
from review_sensei_github.api_run_plan import (
    MIN_OPERATION_REQUESTS,
    TERMINAL_REQUESTS,
    owner_allocations,
)
from review_sensei_github.api_run_requests import ApiRunSessionLedger
from review_sensei_github.broker_client import BrokerClient
from review_sensei_github.broker_control import BrokerDispatchAccounting
from review_sensei_github.session_ledger import parse_session_comment

from review_sensei.api_work import ApiRunAccounting
from review_sensei.baseline import admission_context_from_document
from review_sensei.bounded_evidence import EvidenceReadBudget
from review_sensei.cli import main
from review_sensei.errors import ReviewInputError
from review_sensei.models import ProviderResponse, ReviewResult
from tests import test_api_work_public_review as fixture_module
from tests import test_review_transaction as tx
from tests.fake_github_http import json_response
from tests.test_api_work_persistence import BASE, HEAD, IDENTITY

# Fixed modest fixture: measured work attempts, not a shipping default/minimum.
# Admission 11 + provider metadata 4 + completion 8 + publication 45.
QUALIFIED_WORK_CALLS = 11 + 4 + 8 + 45
QUALIFICATION_LIMIT = HOSTED_API_STARTUP_PLAN.liability + 2 * (
    TERMINAL_REQUESTS + QUALIFIED_WORK_CALLS
)


def caller_measurement(fixture):
    state = fixture.accounted.accounting
    durable = ApiRunAccounting.from_document(
        parse_session_comment(
            fixture.comment["body"], identity=IDENTITY
        ).api_run_accounting
    )
    observed = [o for transport in fixture.transports for o in transport.observations]
    occupied = (
        state.startup.liability
        + state.prior_nonstartup_liability
        + state.confirmed
        + state.uncertain
        + state.reserved_undispatched
    )
    assert occupied + state.remaining == state.configuration.limit
    return {
        "configured_limit": state.configuration.limit,
        "physical_attempts": len(fixture.trace),
        "physical_startup_attempts": sum(o["phase"] == "startup" for o in observed),
        "physical_attempts_by_phase": dict(
            Counter(o["lifecycle_phase"] for o in observed)
        ),
        "observed_unknown_responses": sum(o["outcome"] == "unknown" for o in observed),
        "startup_reservation": state.startup.liability,
        "live_known_confirmed_ordinary": state.confirmed,
        "live_explicit_pending_ordinary": state.uncertain,
        "unconfirmed_native_mutation": fixture.accounted._checkpoint is not None,
        "durable_known_confirmed_ordinary": durable.confirmed,
        "reserved_undispatched": state.reserved_undispatched,
        "predecessor_reserved_opaque": sum(
            b.undispatched + len(b.pending)
            for b in state.blocks
            if b.issued_by != state.owner
        ),
        "free_unallocated": state.remaining,
        "current_terminal_usable": fixture.accounted.usable,
        "terminal_owner": fixture.accounted._terminal_reason,
        "attempt": state.owner.run_attempt,
        "record_phase": parse_session_comment(
            fixture.comment["body"], identity=IDENTITY
        ).transaction.phase
        if parse_session_comment(fixture.comment["body"], identity=IDENTITY).transaction
        is not None
        else None,
        "native_approvals": sum(r["state"] == "APPROVED" for r in fixture.reviews),
        "check_conclusions": [r.get("conclusion") for r in fixture.check_runs],
        "native_walks_current_owner": dict(fixture.accounted.native_walks),
        "worker_internal_observations": list(fixture.worker_observations),
    }


class LifecycleFixture(fixture_module.HostedRunFixture):
    def __init__(self, directory, *, fanout=30, limit=QUALIFICATION_LIMIT):
        super().__init__(directory, limit=limit, fanout=fanout)
        self.plan = HOSTED_API_STARTUP_PLAN
        self.limit = limit
        self.reviews = []
        self.inline = []
        self.check_runs = []
        self.interrupt_engine_read = False
        self.fail_terminal_patch = False
        self.terminal_fault_trace_index = None
        self.worker_observations = []

    def _external_open(self, request, timeout):
        path = urlsplit(request.full_url).path
        method = request.method
        payload = json.loads(request.data) if request.data else None
        if (
            self.fail_terminal_patch
            and method == "PATCH"
            and path.endswith("/issues/comments/1000000")
            and self.accounted._terminal_reason is not None
        ):
            self.fail_terminal_patch = False
            self.trace.append((self.attempt, method, path))
            self.terminal_fault_trace_index = len(self.trace)
            raise TimeoutError("synthetic unknown terminal response")
        if (
            self.interrupt_engine_read
            and path.endswith("/issues/7/comments")
            and method == "GET"
        ):
            self.interrupt_engine_read = False
            self.trace.append((self.attempt, method, path))
            raise KeyboardInterrupt()
        review = path.endswith("/pulls/7/reviews")
        inline = (
            path.endswith("/pulls/7/comments")
            or "/reviews/" in path
            and path.endswith("/comments")
        )
        files = path.endswith("/pulls/7/files")
        check = "/check-runs" in path
        graphql = path == "/graphql"
        broker = urlsplit(request.full_url).hostname == "broker.github.test"
        if review or inline or files or check or graphql or broker:
            assert (
                len(self.accounted.observations)
                == sum(a == self.attempt for a, _, _ in self.trace) + 1
            )
            self.trace.append((self.attempt, method, path))
            if broker:
                self.worker_observations.append(
                    {
                        "capability": payload["capability"],
                        "service_ceiling": payload["control_budget"]["max_dispatches"],
                        "observed_dispatches": min(
                            self.fanout, payload["control_budget"]["max_dispatches"]
                        ),
                    }
                )
                value = {
                    "token": "synthetic-" + payload["capability"],
                    "capability": payload["capability"],
                    "control_receipt": {
                        **payload["control_budget"],
                        "dispatched": min(
                            self.fanout, payload["control_budget"]["max_dispatches"]
                        ),
                    },
                }
                if payload["capability"] == "review_session":
                    value.update(
                        session_state="known" if self.comment else "enrolled",
                        reservation_owner={"run_id": "456", "head_sha": HEAD},
                    )
                return json_response(value)
            if files:
                return json_response(
                    [
                        {
                            "filename": "src/app.py",
                            "patch": "@@ -1 +1,2 @@\n keep\n+change",
                            "additions": 1,
                            "deletions": 0,
                            "status": "modified",
                        }
                    ]
                )
            if inline:
                return json_response(self.inline)
            if graphql:
                return json_response(
                    {
                        "data": {
                            "repository": {
                                "pullRequest": {
                                    "reviewThreads": {
                                        "nodes": [],
                                        "pageInfo": {
                                            "hasNextPage": False,
                                            "endCursor": None,
                                        },
                                    }
                                }
                            }
                        }
                    }
                )
            if review:
                if method == "GET":
                    return json_response(copy.deepcopy(self.reviews))
                bot = {"id": 999, "login": "reviewsensei[bot]", "type": "Bot"}
                item = {
                    "id": 100 + len(self.reviews),
                    "commit_id": payload["commit_id"],
                    "state": "APPROVED"
                    if payload["event"] == "APPROVE"
                    else "COMMENTED",
                    "body": payload["body"],
                    "user": bot,
                    "pull_request_url": "https://api.github.test/repos/owner/repo/pulls/7",
                    "html_url": "https://github.test/reviews/100",
                    "submitted_at": "2026-10-25T22:00:00Z",
                }
                self.reviews.append(item)
                for comment in payload.get("comments", []):
                    self.inline.append(
                        {
                            "id": 200 + len(self.inline),
                            "pull_request_review_id": item["id"],
                            "commit_id": HEAD,
                            "pull_request_url": item["pull_request_url"],
                            "user": bot,
                            **comment,
                        }
                    )
                return json_response(copy.deepcopy(item), 200)
            if check:
                if method == "GET":
                    return json_response({"check_runs": copy.deepcopy(self.check_runs)})
                if method == "POST":
                    item = {
                        "id": 500 + len(self.check_runs),
                        "html_url": "https://github.test/checks/500",
                        "app": {"slug": "reviewsensei"},
                        **payload,
                    }
                    self.check_runs.append(item)
                    return json_response(copy.deepcopy(item), 201)
                item = next(
                    item
                    for item in self.check_runs
                    if item["id"] == int(path.rsplit("/", 1)[1])
                )
                item.update(payload)
                return json_response(copy.deepcopy(item))
        response = super()._external_open(request, timeout)
        if path.endswith("/pulls/7"):
            pr = json.loads(response.read(524288))
            pr.update(user={"login": "alice", "type": "User"}, changed_files=1)
            pr["head"].update(
                ref="feature", repo={"full_name": "owner/repo", "fork": False}
            )
            pr["base"]["repo"]["fork"] = False
            return json_response(pr)
        return response

    def recover_lifecycle(self):
        self.attempt = 2
        native, configuration, evidence, owner = self.prelude()
        loaded = native.load(IDENTITY)
        previous = ApiRunAccounting.from_document(loaded.record.api_run_accounting)
        previous.require_binding(self.accounted.run, configuration)
        stopped = evidence.ceased_owner(IDENTITY, previous.owner)
        owner = replace(owner, generation=previous.owner.generation + 1)

        def mutate(record):
            if (
                record.record_sha256 != loaded.record.record_sha256
                or evidence.ceased_owner(IDENTITY, previous.owner) != stopped
            ):
                raise ReviewInputError("recovery ownership changed")
            state = (
                previous.claim_startup(owner)
                .reserve(
                    "successor-review",
                    owner_allocations(
                        configuration.limit, HOSTED_API_STARTUP_PLAN.liability
                    ).successor_work,
                )
                .reserve("successor-terminal", TERMINAL_REQUESTS)
            )
            return record.evolve(
                generation=record.generation + 1, api_run_accounting=state.to_document()
            )

        self.accounted.reserve_and_activate(
            native, IDENTITY, mutate, block_id="successor-review"
        )
        self.accounted.bind_terminal_reservation("successor-terminal")
        return native


class CallerLifecycleTests(unittest.TestCase):
    def journey(
        self,
        directory,
        *,
        human=False,
        fanout=30,
        prepared=None,
        expected_analysis=0,
        provider_error=False,
        limit=QUALIFICATION_LIMIT,
    ):
        if prepared is None:
            fixture = LifecycleFixture(directory, fanout=fanout, limit=limit)
            native, configuration, _evidence, owner = fixture.prelude()
            fixture.accounted.initialize_and_activate(
                native, IDENTITY, configuration, owner
            )
        else:
            fixture, native = prepared
        ledger = ApiRunSessionLedger(native, fixture.accounted, IDENTITY)
        hooks = ApiRunReviewHooks(ledger, IDENTITY, base_sha=BASE, head_sha=HEAD)
        root = Path(directory)
        args = tx.HostedAnalysisCliTests()._argv(
            root, admission_context_output=root / "admission.json"
        )
        index = args.index("--session-ledger")
        del args[index : index + 2]
        args[args.index("--pull-request") + 1] = str(IDENTITY.pull_request)
        args[args.index("--base-sha") + 1] = BASE
        args[args.index("--head-sha") + 1] = HEAD
        config = root / "trusted.yml"
        config.write_bytes(fixture.config_bytes)
        args.extend(
            [
                "--github-session-ledger",
                "--repository-id",
                str(IDENTITY.repository_id),
                "--config",
                str(config),
                "--format",
                "json",
            ]
        )
        provider_calls = []

        class Provider(tx.HostedAnalysisCliTests._Provider):
            def complete(self, request):
                provider_calls.append(True)
                if provider_error:
                    raise ReviewInputError("synthetic provider failure")
                comments = (
                    []
                    if not human
                    else [
                        {
                            "path": "src/app.py",
                            "line": 2,
                            "side": "RIGHT",
                            "body": "Confirm that this changed path is valid for the external consumer; its contract is not available in this repository.",
                            "severity": "high",
                            "blocking": False,
                            "needs_human": True,
                        }
                    ]
                )
                return ProviderResponse(
                    text=json.dumps(
                        {"summary": "Complete small review.", "comments": comments}
                    ),
                    provider=self.name,
                    model=self.model,
                )

        errors = io.StringIO()
        with (
            patch.dict(
                os.environ,
                {
                    "GITHUB_RUN_ID": "456",
                    "GITHUB_RUN_ATTEMPT": str(fixture.attempt),
                    "REVIEWSENSEI_APP_ID": "999",
                },
            ),
            patch.object(
                EvidenceReadBudget,
                "__init__",
                side_effect=AssertionError("no replacement phase budget"),
            ),
            patch(
                "review_sensei.cli.default_registry",
                return_value=tx.HostedAnalysisCliTests._Registry(Provider()),
            ),
            redirect_stdout(io.StringIO()),
            redirect_stderr(errors),
        ):
            status = main(args, hosted_hooks=hooks)
            self.assertEqual(status, expected_analysis, errors.getvalue())
            if status != 0:
                return fixture, None, provider_calls
            result = ReviewResult.from_dict(
                json.loads((root / "review.json").read_text())
            )
            baseline, key = admission_context_from_document(
                json.loads((root / "admission.json").read_text())
            )
            broker = BrokerClient(
                broker_url="https://broker.github.test/api/github/token",
                opener=fixture.accounted.open,
                control=BrokerDispatchAccounting(
                    fixture.accounted.budget_view, "c" * 64, 10, 1
                ),
            )
            app = GitHubApplication(
                broker=broker,
                http=fixture.http,
                reviewer=ReviewPublisher(http=fixture.http),
                learner=None,
                replier=None,
                session_ledger=ledger,
            )
            publication = hooks.publish_review(
                app,
                options=GitHubWriteOptions(
                    auto_review=True,
                    auto_approve=True,
                    github_writes=True,
                    complete_inventory=True,
                ),
                oidc_token="synthetic-oidc",
                repository=IDENTITY.repository,
                repository_id=IDENTITY.repository_id,
                pull_request=IDENTITY.pull_request,
                head_sha=HEAD,
                base_sha=BASE,
                base_branch="main",
                result=result,
                diff=(root / "review.patch").read_text(),
                app_slug="reviewsensei[bot]",
                configuration_context=json.loads(
                    (root / "configuration.json").read_text()
                ),
                evidence_context={"evidence_policy": "legacy", "snapshot_sha256": None},
                baseline=baseline,
                current_key=key,
                changed_paths=["src/app.py"],
            )
        return fixture, publication, provider_calls

    def test_public_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, publication, calls = self.journey(directory)
            self.assertEqual(len(calls), 1)
            self.assertEqual(
                fixture.accounted.accounting.remaining,
                owner_allocations(
                    fixture.limit, HOSTED_API_STARTUP_PLAN.liability
                ).successor_total,
            )
            self.assertTrue(
                any(r["state"] == "APPROVED" for r in fixture.reviews), publication
            )
            self.assertEqual(
                parse_session_comment(
                    fixture.comment["body"], identity=IDENTITY
                ).transaction.phase,
                "publication_succeeded",
            )

    def test_public_human_decision_withholds_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, publication, calls = self.journey(directory, human=True)
            self.assertEqual(len(calls), 1)
            self.assertFalse(
                any(r["state"] == "APPROVED" for r in fixture.reviews), publication
            )
            self.assertTrue(fixture.reviews)
            self.assertEqual(
                parse_session_comment(
                    fixture.comment["body"], identity=IDENTITY
                ).transaction.phase,
                "publication_succeeded",
            )
            self.assertTrue(
                any(
                    run.get("conclusion") == "action_required"
                    for run in fixture.check_runs
                ),
                fixture.check_runs,
            )

    def test_interruption_recovers_with_protected_successor_capacity(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LifecycleFixture(directory)
            native, configuration, _evidence, owner = fixture.prelude()
            fixture.accounted.initialize_and_activate(
                native, IDENTITY, configuration, owner
            )
            fixture.accounted.replace_checkpoint(native, IDENTITY)
            original = ApiRunAccounting.from_document(
                parse_session_comment(
                    fixture.comment["body"], identity=IDENTITY
                ).api_run_accounting
            )
            first_transport = fixture.accounted
            fixture.interrupt_engine_read = True
            fixture, publication, calls = self.journey(
                directory, prepared=(fixture, native), expected_analysis=130
            )
            self.assertFalse(calls)
            self.assertIsNone(publication)
            self.assertEqual(first_transport.accounting.uncertain, 1)
            native = fixture.recover_lifecycle()
            self.assertEqual(fixture.accounted.accounting.blocks[:2], original.blocks)
            self.assertEqual(fixture.accounted.accounting.remaining, 0)
            self.assertGreaterEqual(
                fixture.accounted.budget_view.calls, original.confirmed
            )
            self.assertEqual(
                fixture.accounted.accounting.completed[: len(original.completed)],
                original.completed,
            )
            fixture, publication, calls = self.journey(
                directory, prepared=(fixture, native)
            )
            self.assertEqual(len(calls), 1)
            self.assertTrue(
                any(r["state"] == "APPROVED" for r in fixture.reviews), publication
            )
            self.assertEqual(fixture.accounted.accounting.blocks[:2], original.blocks)
            self.assertEqual(fixture.accounted.accounting.owner.run_attempt, 2)
            self.recovery_measurement = caller_measurement(fixture)
            self.assertEqual(
                parse_session_comment(
                    fixture.comment["body"], identity=IDENTITY
                ).transaction.phase,
                "publication_succeeded",
            )

    def test_provider_failure_has_one_funded_cleanup_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, publication, calls = self.journey(
                directory, expected_analysis=2, provider_error=True
            )
            record = parse_session_comment(fixture.comment["body"], identity=IDENTITY)
            self.assertEqual(record.failed_attempts, 1)
            self.assertIsNone(record.transaction)
            self.assertIsNone(record.reservation_id)
            self.assertEqual(len(calls), 1)
            self.assertIsNone(publication)
            self.assertEqual(fixture.accounted._terminal_reason, "analysis-abort")
            self.assertEqual(
                fixture.accounted.accounting.remaining,
                owner_allocations(
                    fixture.limit, HOSTED_API_STARTUP_PLAN.liability
                ).successor_total,
            )

    def test_measured_approval_and_withholding_boundaries_are_not_default_selection(
        self,
    ):
        for human, work in (
            (False, QUALIFIED_WORK_CALLS),
            (True, QUALIFIED_WORK_CALLS - 4),
        ):
            # Initial owner receives the odd unit; the successor must work with
            # floor(remaining/2). These are fixed fixture measurements only.
            minimum = (
                HOSTED_API_STARTUP_PLAN.liability + 2 * (TERMINAL_REQUESTS + work) - 1
            )
            for limit in (minimum - 1, minimum):
                with (
                    self.subTest(human=human, limit=limit),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    fixture = LifecycleFixture(directory, limit=limit)
                    native, config, _, owner = fixture.prelude()
                    fixture.accounted.initialize_and_activate(
                        native, IDENTITY, config, owner
                    )
                    if limit < minimum:
                        with self.assertRaisesRegex(ReviewInputError, "cannot fund"):
                            self.journey(
                                directory, prepared=(fixture, native), human=human
                            )
                        self.assertFalse(
                            any(r["state"] == "APPROVED" for r in fixture.reviews)
                        )
                        self.assertEqual(
                            parse_session_comment(
                                fixture.comment["body"], identity=IDENTITY
                            ).transaction.phase,
                            "publication_failed",
                        )
                    else:
                        fixture, publication, _ = self.journey(
                            directory, prepared=(fixture, native), human=human
                        )
                        self.assertEqual(publication.status, "published")
                        self.assertEqual(len(fixture.trace), 78 if human else 82)
                        self.assertEqual(
                            fixture.accounted.accounting.confirmed, 68 if human else 72
                        )
                        self.assertEqual(
                            fixture.accounted.usable, TERMINAL_REQUESTS - 4
                        )

    def test_successor_one_short_keeps_predecessor_capacity_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LifecycleFixture(directory, limit=QUALIFICATION_LIMIT - 1)
            native, config, _, owner = fixture.prelude()
            fixture.accounted.initialize_and_activate(native, IDENTITY, config, owner)
            fixture.accounted.replace_checkpoint(native, IDENTITY)
            original = ApiRunAccounting.from_document(
                parse_session_comment(
                    fixture.comment["body"], identity=IDENTITY
                ).api_run_accounting
            )
            fixture.interrupt_engine_read = True
            self.journey(directory, prepared=(fixture, native), expected_analysis=130)
            native = fixture.recover_lifecycle()
            with self.assertRaisesRegex(ReviewInputError, "cannot fund"):
                self.journey(directory, prepared=(fixture, native))
            self.assertEqual(fixture.accounted.accounting.blocks[:2], original.blocks)
            self.assertEqual(fixture.accounted.accounting.remaining, 0)
            self.assertFalse(any(r["state"] == "APPROVED" for r in fixture.reviews))
            self.assertEqual(len(fixture.trace), 100)

    def test_worker_internal_fanout_does_not_change_public_caller_cost(self):
        observed = []
        for fanout in (9, 12, 30):
            with tempfile.TemporaryDirectory() as directory:
                fixture, publication, calls = self.journey(directory, fanout=fanout)
                self.assertEqual(
                    next(
                        o["observed_dispatches"]
                        for o in fixture.worker_observations
                        if o["capability"] == "review_session"
                    ),
                    fanout,
                )
                self.assertTrue(
                    all(
                        o["observed_dispatches"] <= o["service_ceiling"]
                        for o in fixture.worker_observations
                    )
                )
                observed.append(
                    (
                        len(fixture.trace),
                        fixture.accounted.accounting.confirmed,
                        fixture.accounted.accounting.remaining,
                    )
                )
        self.assertEqual(observed, [observed[0]] * 3)

    def test_unknown_cleanup_write_is_not_repeated_by_nested_error_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LifecycleFixture(directory)
            native, configuration, _evidence, owner = fixture.prelude()
            fixture.accounted.initialize_and_activate(
                native, IDENTITY, configuration, owner
            )
            fixture.fail_terminal_patch = True
            fixture, publication, calls = self.journey(
                directory,
                prepared=(fixture, native),
                expected_analysis=2,
                provider_error=True,
            )
            self.assertEqual(fixture.accounted._terminal_reason, "analysis-abort")
            self.assertEqual(fixture.accounted.accounting.uncertain, 1)
            terminal = [
                o
                for o in fixture.accounted.observations
                if o["lifecycle_phase"] == "terminal"
            ]
            self.assertTrue(terminal)
            # No nested cleanup or new reads after the unknown terminal write.
            self.assertEqual(len(fixture.trace), fixture.terminal_fault_trace_index)
            self.assertEqual(fixture.trace[-1][1], "PATCH")
            self.assertIsNone(publication)
            self.assertEqual(
                parse_session_comment(
                    fixture.comment["body"], identity=IDENTITY
                ).transaction.phase,
                "analysis",
            )

    def test_finite_initial_tranche_and_successor_headroom_at_plan_boundary(self):
        minimum = HOSTED_API_STARTUP_PLAN.liability + 2 * (
            TERMINAL_REQUESTS + MIN_OPERATION_REQUESTS
        )
        for limit in (minimum - 1, minimum, minimum + 1):
            with self.subTest(limit=limit), tempfile.TemporaryDirectory() as directory:
                fixture = LifecycleFixture(directory, limit=limit)
                native, configuration, _evidence, owner = fixture.prelude()
                if limit < minimum:
                    with self.assertRaisesRegex(
                        ReviewInputError, "operation allocation is unfunded"
                    ):
                        fixture.accounted.initialize_and_activate(
                            native, IDENTITY, configuration, owner
                        )
                    self.assertIsNone(fixture.comment)
                    continue
                state = fixture.accounted.initialize_and_activate(
                    native, IDENTITY, configuration, owner
                )
                allocations = owner_allocations(
                    limit, HOSTED_API_STARTUP_PLAN.liability
                )
                self.assertEqual(
                    [b.undispatched for b in state.blocks],
                    [allocations.first_work, allocations.first_terminal],
                )
                self.assertEqual(state.remaining, allocations.successor_total)
                self.assertEqual(state.configuration.limit, limit)

    def test_missing_terminal_binding_refuses_before_provider_and_stays_one_shot(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LifecycleFixture(directory)
            native, configuration, _evidence, owner = fixture.prelude()
            fixture.accounted.initialize_and_activate(
                native, IDENTITY, configuration, owner
            )
            ledger = ApiRunSessionLedger(native, fixture.accounted, IDENTITY)
            hooks = ApiRunReviewHooks(ledger, IDENTITY, base_sha=BASE, head_sha=HEAD)
            fixture.accounted._terminal_block = None
            before = len(fixture.trace)
            with self.assertRaisesRegex(ReviewInputError, "terminal tail"):
                hooks.begin_provider_phase(ledger)
            with self.assertRaisesRegex(ReviewInputError, "terminal completion"):
                ledger.claim_analysis_cleanup(IDENTITY, "synthetic-reservation")
            self.assertFalse(
                ledger.claim_analysis_cleanup(IDENTITY, "synthetic-reservation")
            )
            self.assertEqual(len(fixture.trace), before)
