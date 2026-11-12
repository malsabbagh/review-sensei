from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from email.message import Message
from pathlib import Path
from unittest.mock import call, patch
from urllib.error import HTTPError
from urllib.parse import urlsplit

from review_sensei_github import (
    BrokerClient,
    GitHubBrokerAdmissionHeldError,
    GitHubHTTPTransientError,
)
from review_sensei_github.api_run_plan import owner_allocations
from review_sensei_github.broker_client import BrokerSession
from review_sensei_github.session_ledger import parse_session_comment
from test_action_reply_entry import ReplyEntryFixture
from test_action_run import CommandEntryFixture
from test_review_run import IDENTITY, EntryFixture, fixture_process_environment

from review_sensei import cli as core_cli
from review_sensei.api_work import DEFAULT_API_REQUEST_LIMIT, MAX_API_REQUEST_LIMIT
from review_sensei.bounded_evidence import MAX_PART_READS, EvidenceReadBudget
from review_sensei.concurrency import (
    AdmissionCancelled,
    AdmissionRejected,
    ProviderAdmission,
    ReviewConcurrencyPlan,
)
from review_sensei.errors import EvidenceBudgetExhausted, ReviewInputError
from review_sensei.operational_boundaries import (
    CrossPrConcurrencyComparison,
    QuotaThrottleRecoveryPlan,
    ResourceOwnerBoundary,
    classify_quota_throttle_recovery,
    inspect_resource_owner_boundaries,
    plan_cross_pr_concurrency,
)
from review_sensei.outcomes import ResourceBudget


def _authority(
    *,
    repository: str,
    repository_id: int,
    pull_request: int,
    run_id: str,
    purpose: str = "review",
    head_sha: str = "a" * 40,
) -> dict[str, object]:
    source: dict[str, object]
    if purpose == "review":
        source = {
            "kind": "pull_request",
            "id": pull_request,
            "updated_at": None,
            "body_sha256": None,
        }
    elif purpose == "command":
        source = {
            "kind": "issue_comment",
            "id": 10000 + pull_request,
            "updated_at": "2026-10-26T00:00:00Z",
            "body_sha256": "b" * 64,
        }
    else:
        source = {
            "kind": "review_comment",
            "id": 20000 + pull_request,
            "updated_at": "2026-10-26T00:00:00Z",
            "body_sha256": "c" * 64,
        }
    return {
        "version": 3,
        "purpose": purpose,
        "repository": repository,
        "repository_id": repository_id,
        "pull_request": pull_request,
        "head_sha": head_sha,
        "run_id": run_id,
        "run_attempt": 1,
        "job": "command" if purpose == "command" else "hosted",
        "job_workflow_ref": (
            "malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@refs/tags/v5"
        ),
        "job_workflow_sha": "a" * 40,
        "issued_at": 1_700_000_000,
        "expires_at": 1_700_000_600,
        "audience": "reviewsensei-run-action",
        "source": source,
    }


class _FakeHttpResponse(io.BytesIO):
    def __init__(self, body: bytes, status: int = 200) -> None:
        super().__init__(body)
        self.status = status
        self.reason = "synthetic"
        self.headers: dict[str, str] = {}


class CrossPrQuotaG25Tests(unittest.TestCase):
    def test_per_pr_serialization_and_cross_pr_cross_repo_independence(self) -> None:
        for purpose_a, purpose_b in (
            ("review", "command"),
            ("review", "reply"),
            ("command", "reply"),
        ):
            same = plan_cross_pr_concurrency(
                repository_a="owner/repo",
                pull_request_a=7,
                purpose_a=purpose_a,
                repository_b="owner/repo",
                pull_request_b=7,
                purpose_b=purpose_b,
            )
            self.assertTrue(same.same_pr_serialized)
            self.assertFalse(same.cancel_in_progress)
            self.assertFalse(same.global_cross_repo_lock)
            self.assertFalse(same.shares_credentials_across_runs)
            self.assertEqual(
                same.workflow_group_a, "reviewsensei-provider-review-owner/repo-7"
            )
            self.assertEqual(same.workflow_group_a, same.workflow_group_b)
            self.assertEqual(same.provider_key_a, same.provider_key_b)
            self.assertEqual(
                same.quota_ownership_scope, "per_pr_workflow_serialization"
            )
            self.assertEqual(same.to_dict()["same_pr_serialized"], True)

        sibling_pr = plan_cross_pr_concurrency(
            repository_a="owner/repo",
            pull_request_a=7,
            purpose_a="review",
            repository_b="owner/repo",
            pull_request_b=8,
            purpose_b="command",
        )
        self.assertFalse(sibling_pr.same_pr_serialized)
        self.assertNotEqual(sibling_pr.workflow_group_a, sibling_pr.workflow_group_b)
        self.assertNotEqual(sibling_pr.provider_key_a, sibling_pr.provider_key_b)
        self.assertEqual(
            sibling_pr.quota_ownership_scope,
            "provider_and_installation_account_boundary",
        )

        cross_repo = plan_cross_pr_concurrency(
            repository_a="owner/repo",
            pull_request_a=7,
            purpose_a="review",
            repository_b="other/service",
            pull_request_b=7,
            purpose_b="review",
        )
        self.assertFalse(cross_repo.same_pr_serialized)
        self.assertNotEqual(cross_repo.workflow_group_a, cross_repo.workflow_group_b)
        self.assertNotEqual(cross_repo.provider_key_a, cross_repo.provider_key_b)

        # Length-prefixed core keys prevent repo/PR boundary ambiguity.
        plan_ambig_a = ReviewConcurrencyPlan.for_pull_request("owner/repo-1", 2)
        plan_ambig_b = ReviewConcurrencyPlan.for_pull_request("owner/repo", 12)
        self.assertNotEqual(plan_ambig_a.provider_key, plan_ambig_b.provider_key)
        self.assertNotEqual(plan_ambig_a.workflow_key, plan_ambig_b.workflow_key)

    def test_in_process_provider_admission_fairness_and_bounded_cleanup(self) -> None:
        plan_pr7 = ReviewConcurrencyPlan.for_pull_request("owner/repo", 7)
        plan_pr8 = ReviewConcurrencyPlan.for_pull_request("owner/repo", 8)
        assert plan_pr7.provider is not None and plan_pr8.provider is not None

        gate_pr7 = ProviderAdmission(
            plan_pr7.provider,
            max_waiters=1,
            waiter_poll_interval=0.01,
        )
        gate_pr8 = ProviderAdmission(
            plan_pr8.provider,
            max_waiters=1,
            waiter_poll_interval=0.01,
        )

        lease_pr7_first = gate_pr7.acquire(timeout=1.0)
        # Sibling PR 8 acquires immediately without waiting on PR 7.
        lease_pr8 = gate_pr8.acquire(timeout=0.05)
        self.assertEqual(gate_pr8.snapshot().active, 1)
        lease_pr8.release()
        self.assertEqual(gate_pr8.snapshot().active, 0)

        waiter_entered = threading.Event()
        cancel_waiter = threading.Event()
        waiter_errors: list[BaseException] = []

        def blocked_waiter() -> None:
            waiter_entered.set()
            try:
                gate_pr7.acquire(timeout=2.0, cancel_event=cancel_waiter)
            except BaseException as exc:  # noqa: BLE001
                waiter_errors.append(exc)

        thread = threading.Thread(target=blocked_waiter)
        thread.start()
        self.assertTrue(waiter_entered.wait(timeout=1.0))
        wait_deadline = time.monotonic() + 2.0
        while (
            gate_pr7.snapshot().waiters == 0
            and thread.is_alive()
            and time.monotonic() < wait_deadline
        ):
            threading.Event().wait(0.005)
        self.assertEqual(
            gate_pr7.snapshot().waiters,
            1,
            "queued waiter failed to register within bounded deadline",
        )

        # Third caller on the same PR exceeds bounded waiter capacity and fails closed.
        with self.assertRaises(AdmissionRejected):
            gate_pr7.acquire(timeout=0.1)

        # Cancelling the queued waiter cleans up reservation counts deterministically.
        cancel_waiter.set()
        thread.join(timeout=1.0)
        self.assertEqual(len(waiter_errors), 1)
        self.assertIsInstance(waiter_errors[0], AdmissionCancelled)
        self.assertEqual(gate_pr7.snapshot().waiters, 0)
        self.assertEqual(gate_pr7.snapshot().active, 1)

        lease_pr7_first.release(outcome="failed")
        self.assertEqual(gate_pr7.snapshot().active, 0)
        self.assertEqual(gate_pr7.snapshot().waiters, 0)

    def test_simultaneous_multi_pr_broker_rate_limit_containment_and_recovery(
        self,
    ) -> None:
        lock = threading.Lock()
        dispatches_by_run: dict[str, int] = {}
        # One synthetic installation/account boundary, not one limiter per PR.
        shared_owner = 12345
        charged_requests: list[tuple[int, int, int, str]] = []
        remaining_owner_grants = 2
        owner_holds = 0

        def shared_broker_opener(request, timeout):
            nonlocal remaining_owner_grants, owner_holds
            del timeout
            payload = json.loads(request.data.decode("utf-8"))
            att = payload["session_attestation"]
            run_id = str(att["run_id"])
            with lock:
                dispatches_by_run[run_id] = dispatches_by_run.get(run_id, 0) + 1
                charged_requests.append(
                    (shared_owner, att["repository_id"], att["pull_request"], run_id)
                )
                # Synthetic account quota is shared by all four identities.
                # Arrival order chooses admission, not a hard-coded run ID.
                if remaining_owner_grants:
                    remaining_owner_grants -= 1
                    forced_code = None
                else:
                    owner_holds += 1
                    forced_code = 503 if owner_holds == 1 else 429
            if forced_code == 503:
                body = json.dumps(
                    {"error": "broker_admission_held", "temporary": True}
                ).encode("utf-8")
                raise HTTPError(
                    request.full_url,
                    503,
                    "Service Unavailable",
                    Message(),
                    _FakeHttpResponse(body, 503),
                )
            if forced_code == 429:
                body = json.dumps({"error": "rate_limited"}).encode("utf-8")
                raise HTTPError(
                    request.full_url,
                    429,
                    "Too Many Requests",
                    Message(),
                    _FakeHttpResponse(body, 429),
                )
            actor = (
                None
                if att["purpose"] == "review"
                else {"login": "octocat", "type": "User", "association": "OWNER"}
            )
            response_body = json.dumps(
                {
                    "token": f"ghs_isolated_{att['repository_id']}_{att['pull_request']}_{run_id}",
                    "capability": "review_session",
                    "session_state": "known",
                    "app_id": shared_owner,
                    "reservation_owner": {
                        "run_id": run_id,
                        "head_sha": att["head_sha"],
                    },
                    "session_grant": f"sg1.1780000000000.{run_id.zfill(43)}",
                    "session_attestation": {**att, "actor": actor},
                }
            ).encode("utf-8")
            return _FakeHttpResponse(response_body, 200)

        workloads = [
            ("owner/repo", 101, 7, "101", "review"),
            ("owner/repo", 101, 8, "202", "command"),
            ("other/service", 202, 7, "303", "reply"),
            ("other/service", 202, 9, "404", "review"),
        ]

        def execute_one(
            spec: tuple[str, int, int, str, str],
        ) -> tuple[str, object]:
            repo, repo_id, pr_num, run_id, purpose = spec
            auth = _authority(
                repository=repo,
                repository_id=repo_id,
                pull_request=pr_num,
                run_id=run_id,
                purpose=purpose,
            )
            client = BrokerClient(
                broker_url="https://broker.reviewsensei.dev/api/github/token",
                authority=auth,
                opener=shared_broker_opener,
            )
            try:
                session = client.open_session(
                    f"oidc.{run_id}",
                    repository_id=repo_id,
                    pull_request=pr_num,
                    head_sha="a" * 40,
                )
                return run_id, session
            except Exception as exc:  # noqa: BLE001
                return run_id, exc

        with ThreadPoolExecutor(max_workers=4) as pool:
            wave_one = dict(pool.map(execute_one, workloads))

        # Exactly two identities consume the shared owner allowance. The other
        # two receive typed holds; do not prescribe a cross-PR fairness order.
        sessions = [
            value for value in wave_one.values() if isinstance(value, BrokerSession)
        ]
        held_runs = {
            run for run, value in wave_one.items() if isinstance(value, Exception)
        }
        self.assertEqual(len(sessions), 2)
        self.assertEqual(len({session.token for session in sessions}), 2)
        self.assertEqual(remaining_owner_grants, 0)
        self.assertEqual(owner_holds, 2)
        self.assertEqual(
            {type(wave_one[run]) for run in held_runs},
            {GitHubBrokerAdmissionHeldError, GitHubHTTPTransientError},
        )
        for session in sessions:
            self.assertNotIn("ghs_isolated_", repr(session))
            self.assertNotIn("sg1.", repr(session))
        self.assertEqual(dispatches_by_run, {"101": 1, "202": 1, "303": 1, "404": 1})

        self.assertEqual(
            set(charged_requests),
            {(shared_owner, repo_id, pr, run) for _, repo_id, pr, run, _ in workloads},
        )
        self.assertEqual(len(charged_requests), 4)

        # Recovery wave: once rate-limit window clears, throttled runs succeed cleanly.
        remaining_owner_grants = 2
        for spec in (item for item in workloads if item[3] in held_runs):
            repo_name, repo_id, pr_num, expected_run_id, _purpose = spec
            run_id, recovered = execute_one(spec)
            self.assertEqual(run_id, expected_run_id)
            self.assertIsInstance(recovered, BrokerSession)
            assert isinstance(recovered, BrokerSession)
            assert recovered.attestation is not None
            assert recovered.reservation_owner is not None
            self.assertEqual(recovered.attestation["run_id"], run_id)
            self.assertEqual(recovered.attestation["repository"], repo_name)
            self.assertEqual(recovered.attestation["repository_id"], repo_id)
            self.assertEqual(recovered.attestation["pull_request"], pr_num)
            self.assertEqual(recovered.reservation_owner["run_id"], run_id)
            self.assertEqual(dispatches_by_run[run_id], 2)
        self.assertEqual(remaining_owner_grants, 0)
        self.assertEqual(len(charged_requests), 6)
        self.assertEqual(
            {key: charged_requests.count(key) for key in set(charged_requests)},
            {
                (shared_owner, repo_id, pr, run): (2 if run in held_runs else 1)
                for _, repo_id, pr, run, _ in workloads
            },
        )

    def test_hosted_bootstrap_throttle_leaves_zero_ledger_state_and_recovers(
        self,
    ) -> None:
        for status_code, payload in (
            (503, {"error": "broker_admission_held", "temporary": True}),
            (429, {"error": "rate_limited"}),
        ):
            with (
                self.subTest(status_code=status_code),
                tempfile.TemporaryDirectory() as dir_throttled,
                tempfile.TemporaryDirectory() as dir_sibling,
            ):
                pr_throttled = EntryFixture(dir_throttled, limit=512)
                pr_sibling = EntryFixture(dir_sibling, limit=512)
                original_external = pr_throttled.external
                throttle_active = True

                def throttled_broker_external(request, timeout):
                    if (
                        throttle_active
                        and urlsplit(request.full_url).hostname == "broker.github.test"
                    ):
                        pr_throttled.trace.append(
                            (
                                pr_throttled.attempt,
                                request.get_method(),
                                urlsplit(request.full_url).path,
                            )
                        )
                        raise HTTPError(
                            request.full_url,
                            status_code,
                            "throttled",
                            Message(),
                            _FakeHttpResponse(
                                json.dumps(payload).encode("utf-8"), status_code
                            ),
                        )
                    return original_external(request, timeout)

                pr_throttled.external = throttled_broker_external  # type: ignore[method-assign]

                # An independent same-target fixture is the unthrottled control.
                # Distinct numeric cross-PR/repository identities are exercised above.
                self.assertEqual(pr_sibling.invoke(), 0, pr_sibling.error)
                self.assertEqual(pr_sibling.provider_calls, 1)
                self.assertEqual(pr_throttled.invoke(), 1)
                self.assertEqual(pr_throttled.provider_calls, 0)
                self.assertIsNone(pr_throttled.comment)

                plan = classify_quota_throttle_recovery(
                    status_code=status_code,
                    stage="broker_admission_before_work",
                    result_durably_retained=False,
                )
                self.assertEqual(plan.recovery_mode, "fresh_bootstrap_retry")
                self.assertFalse(plan.completed_inference_replayed_without_spend)

                # Once cooldown clears, a fresh run succeeds cleanly.
                throttle_active = False
                pr_throttled.run_id += 1
                pr_throttled.trace.clear()
                pr_throttled.transports.clear()
                pr_throttled.worker_observations.clear()
                self.assertEqual(pr_throttled.invoke(), 0, pr_throttled.error)
                self.assertEqual(pr_throttled.provider_calls, 1)
                settled = parse_session_comment(
                    pr_throttled.comment["body"], identity=IDENTITY
                )
                self.assertEqual(settled.transaction.phase, "publication_succeeded")

    def test_post_retention_http_429_replays_publication_with_zero_duplicate_inference(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as dir_a,
            tempfile.TemporaryDirectory() as dir_b,
        ):
            pr_a = EntryFixture(dir_a, limit=512)
            pr_b = EntryFixture(dir_b, limit=512)

            def rate_limited_before_publish() -> None:
                raise GitHubHTTPTransientError("GitHub API returned HTTP 429")

            pr_a.before_publication = rate_limited_before_publish

            # Independent same-target control; this is not cross-PR quota evidence.
            self.assertEqual(pr_b.invoke(), 0, pr_b.error)
            self.assertEqual(pr_b.provider_calls, 1)

            # PR A finishes inference, retains transaction, and fails closed on 429.
            self.assertEqual(pr_a.invoke(), 1)
            self.assertEqual(pr_a.provider_calls, 1)
            self.assertEqual(pr_a.reviews, [])
            retained = parse_session_comment(pr_a.comment["body"], identity=IDENTITY)
            self.assertEqual(retained.transaction.phase, "publication_pending")

            recovery_plan = classify_quota_throttle_recovery(
                status_code=429,
                stage="post_retention_publication",
                result_durably_retained=True,
            )
            self.assertEqual(
                recovery_plan.recovery_mode,
                "retained_publication_replay_zero_inference",
            )
            self.assertTrue(recovery_plan.completed_inference_replayed_without_spend)
            self.assertEqual(
                recovery_plan.to_dict()["recovery_mode"],
                "retained_publication_replay_zero_inference",
            )

            # Attempt 2 after cooldown publishes retained result with zero new inference.
            pr_a.attempt = 2
            pr_a.before_publication = None
            self.assertEqual(pr_a.invoke(), 0, pr_a.error)
            self.assertEqual(pr_a.provider_calls, 1)
            final = parse_session_comment(pr_a.comment["body"], identity=IDENTITY)
            self.assertEqual(
                final.transaction.transaction_id, retained.transaction.transaction_id
            )
            self.assertEqual(final.transaction.phase, "publication_succeeded")
            self.assertEqual(
                sum(review["state"] == "APPROVED" for review in pr_a.reviews), 1
            )

    def test_quota_and_concurrency_contract_validation_guards(self) -> None:
        provider_plan = classify_quota_throttle_recovery(
            status_code=429,
            stage="provider_inference",
            result_durably_retained=True,
        )
        self.assertEqual(
            provider_plan.recovery_mode, "partial_or_fresh_rescan_after_cooldown"
        )
        self.assertFalse(provider_plan.completed_inference_replayed_without_spend)

        with self.assertRaises(ReviewInputError):
            plan_cross_pr_concurrency(
                repository_a="owner/repo",
                pull_request_a=7,
                purpose_a="invalid",
                repository_b="owner/repo",
                pull_request_b=7,
                purpose_b="review",
            )
        with self.assertRaises(ReviewInputError):
            CrossPrConcurrencyComparison(
                workflow_group_a="g1",
                workflow_group_b="g1",
                provider_key_a="p1",
                provider_key_b="p1",
                same_pr_serialized=True,
                cancel_in_progress=True,
                global_cross_repo_lock=False,
                shares_credentials_across_runs=False,
                quota_ownership_scope="per_pr_workflow_serialization",
                diagnostic="bad cancel_in_progress",
            )
        with self.assertRaises(ReviewInputError):
            CrossPrConcurrencyComparison(
                workflow_group_a="g1",
                workflow_group_b="g2",
                provider_key_a="p1",
                provider_key_b="p2",
                same_pr_serialized=False,
                cancel_in_progress=False,
                global_cross_repo_lock=True,
                shares_credentials_across_runs=False,
                quota_ownership_scope="provider_and_installation_account_boundary",
                diagnostic="bad global lock",
            )
        with self.assertRaises(ReviewInputError):
            CrossPrConcurrencyComparison(
                workflow_group_a="g1",
                workflow_group_b="g2",
                provider_key_a="p1",
                provider_key_b="p2",
                same_pr_serialized=False,
                cancel_in_progress=False,
                global_cross_repo_lock=False,
                shares_credentials_across_runs=True,
                quota_ownership_scope="provider_and_installation_account_boundary",
                diagnostic="bad credential sharing",
            )
        with self.assertRaises(ReviewInputError):
            CrossPrConcurrencyComparison(
                workflow_group_a="g1",
                workflow_group_b="g2",
                provider_key_a="p1",
                provider_key_b="p2",
                same_pr_serialized=True,
                cancel_in_progress=False,
                global_cross_repo_lock=False,
                shares_credentials_across_runs=False,
                quota_ownership_scope="per_pr_workflow_serialization",
                diagnostic="mismatched equality",
            )
        for bad_kwargs in (
            {
                "status_code": 500,
                "stage": "provider_inference",
                "result_durably_retained": False,
            },
            {
                "status_code": 429,
                "stage": "unknown_stage",
                "result_durably_retained": False,
            },
            {
                "status_code": 429,
                "stage": "post_retention_publication",
                "result_durably_retained": False,
            },
            {
                "status_code": 503,
                "stage": "broker_admission_before_work",
                "result_durably_retained": True,
            },
        ):
            with self.assertRaises(ReviewInputError):
                classify_quota_throttle_recovery(**bad_kwargs)  # type: ignore[arg-type]

        with self.assertRaises(ReviewInputError):
            QuotaThrottleRecoveryPlan(
                status_code=429,
                stage="provider_inference",
                recovery_mode="partial_or_fresh_rescan_after_cooldown",
                result_durably_retained=False,
                reservation_cleaned_or_bounded=True,
                completed_inference_replayed_without_spend=False,
                duplicates_dispatch=True,
                expands_resource_budget=False,
                shares_credentials_across_prs=False,
                disguises_starvation_as_success=False,
                recovery_instruction="retry",
                diagnostic="invalid duplicate dispatch",
            )
        with self.assertRaises(ReviewInputError):
            QuotaThrottleRecoveryPlan(
                status_code=429,
                stage="provider_inference",
                recovery_mode="partial_or_fresh_rescan_after_cooldown",
                result_durably_retained=False,
                reservation_cleaned_or_bounded=True,
                completed_inference_replayed_without_spend=True,
                duplicates_dispatch=False,
                expands_resource_budget=False,
                shares_credentials_across_prs=False,
                disguises_starvation_as_success=False,
                recovery_instruction="retry",
                diagnostic="replay without retention",
            )

    def test_resource_owner_boundaries_caller_tranches_entry_isolation_and_manual_cli_throttling(
        self,
    ) -> None:
        """Exercise G25 resource-owner contracts, caller HTTP tranche conservation, entry-route isolation, and manual CLI 429/503 fail-closed behavior."""
        boundaries = {b.boundary_id: b for b in inspect_resource_owner_boundaries()}
        self.assertEqual(
            set(boundaries),
            {
                "per_pr_workflow_group",
                "per_run_caller_http_allowance",
                "per_operation_broker_control_budget",
                "github_installation_rate_limit",
                "provider_account_or_local_endpoint",
            },
        )
        for boundary in boundaries.values():
            self.assertFalse(boundary.invents_new_service)
            self.assertFalse(boundary.expands_allowance)
        self.assertEqual(
            boundaries["per_run_caller_http_allowance"].default_limit,
            DEFAULT_API_REQUEST_LIMIT,
        )
        with self.assertRaises(ReviewInputError):
            ResourceOwnerBoundary(
                boundary_id="bad_service",
                owner_scope="single_pull_request",
                default_limit=1,
                hard_ceiling=1,
                invents_new_service=True,
                expands_allowance=False,
                source_symbol="bad:1",
            )
        with self.assertRaises(ReviewInputError):
            ResourceOwnerBoundary(
                boundary_id="bad_scope",
                owner_scope="global_cluster_lock",
                default_limit=1,
                hard_ceiling=1,
                invents_new_service=False,
                expands_allowance=False,
                source_symbol="bad:1",
            )
        with self.assertRaises(ReviewInputError):
            ResourceOwnerBoundary(
                boundary_id="bad_ceiling",
                owner_scope="single_pull_request",
                default_limit=10,
                hard_ceiling=5,
                invents_new_service=False,
                expands_allowance=False,
                source_symbol="bad:1",
            )

        # 1. Exact caller HTTP request conservation (`owner_allocations` & `EvidenceReadBudget`):
        for startup_cost in (0, 7, 16):
            alloc = owner_allocations(DEFAULT_API_REQUEST_LIMIT, startup_cost)
            self.assertEqual(
                startup_cost + alloc.first_total + alloc.successor_total,
                DEFAULT_API_REQUEST_LIMIT,
            )
            self.assertGreater(alloc.first_work, 0)
            self.assertGreater(alloc.successor_work, 0)
        with self.assertRaises(ReviewInputError):
            owner_allocations(MAX_API_REQUEST_LIMIT + 1, 0)

        finite_read_budget = EvidenceReadBudget()
        # 4 calls are reserved until finalization out of MAX_PART_READS (64):
        for _ in range(MAX_PART_READS - 4):
            finite_read_budget.consume()
        with self.assertRaises(EvidenceBudgetExhausted):
            finite_read_budget.consume()
        for _ in range(4):
            finite_read_budget.consume(fence=True)
        with self.assertRaises(EvidenceBudgetExhausted):
            finite_read_budget.consume(fence=True)

        # 2. Independent same-target command/review/reply entry fixtures.
        # These assert route behavior, not cross-PR isolation: the shared broker
        # test above exercises different numeric repository/PR identities.
        with (
            tempfile.TemporaryDirectory() as dir_cmd,
            tempfile.TemporaryDirectory() as dir_rev,
            tempfile.TemporaryDirectory() as dir_rep,
        ):
            pr_cmd = CommandEntryFixture(dir_cmd)
            pr_rev = EntryFixture(dir_rev)
            pr_rep = ReplyEntryFixture(dir_rep)
            pr_rev.run_id = pr_cmd.run_id + 10
            pr_rep.run_id = pr_cmd.run_id + 20

            self.assertEqual(
                pr_cmd.invoke_command("@reviewsensei review pause"), 0, pr_cmd.error
            )
            self.assertEqual(pr_rev.invoke(), 0, pr_rev.error)
            self.assertEqual(pr_rep.invoke_reply(), 0, pr_rep.error)

            assert (
                pr_cmd.comment is not None
                and pr_rev.comment is not None
                and pr_rep.comment is not None
            )
            rec_cmd = parse_session_comment(pr_cmd.comment["body"], identity=IDENTITY)
            rec_rev = parse_session_comment(pr_rev.comment["body"], identity=IDENTITY)
            rec_rep = parse_session_comment(pr_rep.comment["body"], identity=IDENTITY)
            assert (
                rec_cmd is not None
                and rec_rev is not None
                and rec_rep is not None
                and rec_rev.transaction is not None
            )
            self.assertTrue(rec_cmd.operator_paused)
            self.assertFalse(rec_rev.operator_paused)
            self.assertEqual(rec_rev.transaction.phase, "publication_succeeded")
            self.assertEqual(pr_rep.reply_posts, 1)
            self.assertEqual(
                len(
                    {
                        rec_cmd.record_sha256,
                        rec_rev.record_sha256,
                        rec_rep.record_sha256,
                    }
                ),
                3,
            )

        # 3. Manual CLI (`review_sensei.cli.main`) provider HTTP 429 & 503 fail-closed refusal:
        sample_diff = (
            "diff --git a/src/app.py b/src/app.py\n"
            "--- a/src/app.py\n"
            "+++ b/src/app.py\n"
            "@@ -1 +1,2 @@\n"
            " x = 1\n"
            "+y = 2\n"
        )
        for status_code in (429, 503):
            with (
                self.subTest(manual_cli_status=status_code),
                tempfile.TemporaryDirectory() as cli_dir,
            ):
                diff_path = Path(cli_dir) / "change.patch"
                cfg_path = Path(cli_dir) / ".reviewsensei.yml"
                out_path = Path(cli_dir) / "out.json"
                outcome_path = Path(cli_dir) / "outcome.json"
                diff_path.write_text(sample_diff, encoding="utf-8", newline="\n")
                self.assertEqual(diff_path.read_bytes(), sample_diff.encode("utf-8"))
                cfg_path.write_text(
                    "schema: 1\ninference:\n  backend: local-ollama\n  model: qwen2.5-coder:7b\n",
                    encoding="utf-8",
                    newline="\n",
                )
                attempts = 0

                class _ThrottledOpener:
                    def open(self, req_obj: object, timeout: object = None) -> object:
                        nonlocal attempts
                        attempts += 1
                        url = getattr(
                            req_obj, "full_url", "http://127.0.0.1:11434/api/generate"
                        )
                        headers = Message()
                        headers["Retry-After"] = "1"
                        raise HTTPError(
                            url,
                            status_code,
                            "throttled",
                            hdrs=headers,
                            fp=io.BytesIO(b'{"error":"rate limited"}'),
                        )

                err_buf = io.StringIO()
                with (
                    patch.dict(os.environ, fixture_process_environment(), clear=True),
                    patch(
                        "review_sensei.providers.ollama.build_opener",
                        return_value=_ThrottledOpener(),
                    ),
                    patch("time.sleep", return_value=None) as sleeper,
                    contextlib.redirect_stderr(err_buf),
                ):
                    rc_manual = core_cli.main(
                        [
                            "--diff",
                            str(diff_path),
                            "--config",
                            str(cfg_path),
                            "--format",
                            "json",
                            "--output",
                            str(out_path),
                            "--outcome",
                            str(outcome_path),
                            "--exit-semantics",
                            "operational",
                        ]
                    )
                self.assertNotEqual(rc_manual, 0)
                budget = ResourceBudget.create()
                self.assertEqual(attempts, 1 + budget.max_retry_attempts)
                outcome = json.loads(outcome_path.read_text(encoding="utf-8"))
                self.assertEqual(outcome["provider_calls"], attempts)
                self.assertEqual(outcome["retry_attempts"], attempts - 1)
                self.assertEqual(outcome["status"], "budget_exhausted")
                self.assertEqual(outcome["diagnostic"], "transport_retry_exhausted")
                self.assertEqual(
                    sleeper.call_args_list, [call(1.0)] * budget.max_retry_attempts
                )
                self.assertIn(f"HTTP {status_code}", err_buf.getvalue())
                self.assertFalse(out_path.exists())


if __name__ == "__main__":
    unittest.main()
