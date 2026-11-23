"""Installed-wheel qualification across integrated PR #482-#492 gap contracts."""

from __future__ import annotations

import copy
import json
import random
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from packaging_guard import assert_distribution_import, is_disarmed  # noqa: E402

assert_distribution_import()

import review_sensei_github  # noqa: E402

if not is_disarmed() and not Path(
    review_sensei_github.__file__
).resolve().is_relative_to(Path(sys.prefix).resolve()):
    raise ImportError(
        "integrated gap qualification requires the installed hosted adapter"
    )

from fake_github_http import json_response  # noqa: E402
from review_sensei_github.action_receipt import plan_plain_receipt_pages  # noqa: E402
from review_sensei_github.action_representability import (  # noqa: E402
    _command_pricing,
    _plain_multipage_counts,
)
from review_sensei_github.api_run_owner import (  # noqa: E402
    MAX_LATER_ATTEMPT_INSPECTION,
    ApiRunActionsEvidence,
    later_attempt_proved_no_business_entry,
)
from review_sensei_github.api_run_plan import NATIVE_OPERATION_REQUESTS  # noqa: E402
from review_sensei_github.application import (  # noqa: E402
    GitHubApplication,
    GitHubWriteOptions,
    _RecordedHumanAssessmentReply,
)
from review_sensei_github.conversation import (  # noqa: E402
    ConversationPublisher,
    PreparedConversation,
    ReplyResult,
)
from review_sensei_github.errors import GitHubHTTPResponseTooLargeError  # noqa: E402
from review_sensei_github.http import (  # noqa: E402
    MAX_GITHUB_RESPONSE_BYTES,
    MAX_PAGINATION_PAGES,
    GitHubHttp,
)
from review_sensei_github.pre_entry_abort_recovery import (  # noqa: E402
    _probe_owner_ceased,
)
from review_sensei_github.publication import ReviewPublisher  # noqa: E402
from review_sensei_github.publication_failure_recovery import (  # noqa: E402
    PreExternalWriteSettlementWitness,
    iter_step_down_pages,
)
from review_sensei_github.recovery_route_advisor import (  # noqa: E402
    _inspect_pre_write_census_readonly,
    classify_live_ledger_recovery,
    classify_recovery_state,
)
from review_sensei_github.session_ledger import (  # noqa: E402
    GitHubIssueCommentSessionLedger,
    render_session_comment,
)
from review_sensei_github.unstarted_action_recovery import (  # noqa: E402
    _scoped_rollover_cessation,
    _verify_cross_run_cessation_once,
)

from review_sensei.api_work import (  # noqa: E402
    ApiExecutionOwner,
    ApiRunAccounting,
    ApiRunIdentity,
    FrozenApiConfiguration,
    StartupPlan,
    StartupSlot,
    initial_accounting,
)
from review_sensei.bounded_evidence import EvidenceReadBudget  # noqa: E402
from review_sensei.budgets import (  # noqa: E402
    LEGACY_ASSESSMENT_PROMPT_BYTES,
    ReviewWorkBudgets,
)
from review_sensei.diff import analyze_diff  # noqa: E402
from review_sensei.errors import EvidenceBudgetExhausted, ReviewInputError  # noqa: E402
from review_sensei.finding_lifecycle_store import (  # noqa: E402
    preflight_finding_lifecycle,
    read_finding_lifecycle,
    stage_finding_lifecycle,
)
from review_sensei.human_assessment import (  # noqa: E402
    HumanAssessmentDecision,
    HumanAssessmentReply,
    HumanReviewFinding,
    PendingHumanReview,
)
from review_sensei.models import (  # noqa: E402
    ConversationContext,
    ProviderResponse,
    ReviewRequest,
    ReviewTransaction,
)
from review_sensei.outcomes import ResourceBudget  # noqa: E402
from review_sensei.planning import _reconcile_file_outcomes_from_hunks  # noqa: E402
from review_sensei.service import (  # noqa: E402
    _PROVIDER_OUTPUT_CORRECTION,
    ReviewService,
)
from review_sensei.session import (  # noqa: E402
    LocalSessionLedger,
    SessionIdentity,
    SessionRecord,
    session_reservation_id,
)
from review_sensei.writer_containment import LIVE_CANONICAL_CUTOVER_ENABLED  # noqa: E402

BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40
POLICY_SHA = "1" * 40
IDENTITY = SessionIdentity(repository="owner/repo", pull_request=7, repository_id=123)
APP_AUTHOR = {"id": 999, "login": "reviewsensei[bot]", "type": "Bot"}


def _base_accounting(*, run_id: int = 1001, limit: int = 512) -> ApiRunAccounting:
    ident = ApiRunIdentity(123, run_id)
    cfg = FrozenApiConfiguration(commit=POLICY_SHA, digest="1" * 64, limit=limit)
    plan = StartupPlan(
        (
            StartupSlot("hosted", 1, ("s1", "s2")),
            StartupSlot("hosted", 2, ("s3", "s4")),
        )
    )
    owner = ApiExecutionOwner(run_attempt=1, job="hosted", generation=0)
    return initial_accounting(
        ident,
        cfg,
        owner,
        plan,
        prior_nonstartup_liability_upper_bound=5,
    ).reserve("hosted-review", 20)


def _attempt1_jobs(
    run_id: int, *, job: str = "hosted", conclusion: str = "failure"
) -> list[dict[str, object]]:
    return [
        {
            "id": run_id * 100 + 1,
            "run_id": run_id,
            "run_attempt": 1,
            "name": f"ReviewSensei / {job}",
            "status": "completed",
            "conclusion": conclusion,
            "completed_at": "2026-11-22T12:00:00Z",
        }
    ]


def _skipped_step_job(
    *,
    job_id: int,
    run_id: int,
    attempt: int,
    job: str = "hosted",
    step_name: str = "Run hosted review",
) -> dict[str, object]:
    return {
        "id": job_id,
        "run_id": run_id,
        "run_attempt": attempt,
        "name": f"ReviewSensei / {job}",
        "status": "completed",
        "conclusion": "failure",
        "completed_at": "2026-11-22T12:05:00Z",
        "steps": [
            {
                "number": 1,
                "name": "Validate trusted workflow inputs",
                "status": "completed",
                "conclusion": "failure",
            },
            {
                "number": 2,
                "name": step_name,
                "status": "completed",
                "conclusion": "skipped",
            },
        ],
    }


def _make_record(
    *,
    accounting: ApiRunAccounting | None,
    transaction: ReviewTransaction | None = None,
    generation: int = 2,
) -> SessionRecord:
    return SessionRecord._construct(
        repository="owner/repo",
        pull_request=7,
        repository_id=123,
        completed_initial_reviews=0,
        completed_verification_rounds=0,
        failed_attempts=0,
        generation=generation,
        reservation_id=None,
        reserved_slot=None,
        last_committed_reservation_id=(
            transaction.reservation_id if transaction is not None else None
        ),
        created_at="2026-11-10T12:00:00Z",
        updated_at="2026-11-10T12:00:00Z",
        expires_at="2026-12-10T12:00:00Z",
        operator_paused=False,
        transaction=transaction,
        api_run_accounting=accounting.to_document() if accounting is not None else None,
    )


class InstalledBoundaryPreservationTests(unittest.TestCase):
    """Ensure security and API capacity limits remain unchanged after PR #490-#492."""

    def test_boundary_constants_remain_unchanged(self) -> None:
        self.assertEqual(NATIVE_OPERATION_REQUESTS, 10)
        self.assertEqual(MAX_PAGINATION_PAGES, 10)
        self.assertEqual(MAX_GITHUB_RESPONSE_BYTES, 512 * 1024)
        self.assertFalse(LIVE_CANONICAL_CUTOVER_ENABLED)


class InstalledPR490CompositionTests(unittest.TestCase):
    """Qualify PR #490 step-down paging, lifecycle sizing, receipt planning, and advisor."""

    def test_iter_step_down_pages_recovers_within_limits_and_fails_closed_when_starved(
        self,
    ) -> None:
        class _FakePagedHttp:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def evidence_monotonic(self) -> float:
                return 0.0

            def request(
                self, method: str, url: str, *, token: str, timeout_seconds: float
            ) -> tuple[int, object]:
                _ = method, token, timeout_seconds
                self.calls.append(url)
                qs = parse_qs(urlsplit(url).query)
                per_page = int(qs["per_page"][0])
                page = int(qs["page"][0])
                if per_page == 100:
                    raise GitHubHTTPResponseTooLargeError("too large")
                if per_page == 20 and page == 1:
                    return 200, [{"id": i} for i in range(1, 21)]
                return 200, [{"id": i} for i in range(21, 26)]

        protect_calls = 0

        def count_protect() -> None:
            nonlocal protect_calls
            protect_calls += 1

        http = _FakePagedHttp()
        rows_out = list(
            iter_step_down_pages(
                http=http,
                token="t",
                path="/repos/owner/repo/issues/7/comments",
                deadline=30.0,
                protect_request=count_protect,
                hold=ReviewInputError,
                unavailable_message="unavailable",
                incomplete_message="incomplete",
                deadline_message="deadline reached",
                pagination_bound_message="pagination bound reached",
            )
        )
        self.assertEqual([len(chunk) for chunk in rows_out], [20, 5])
        self.assertEqual(protect_calls, 3)

        base_budget = EvidenceReadBudget()
        snap = base_budget.snapshot()
        starved_budget = EvidenceReadBudget(
            snapshot={
                **snap,
                "calls": base_budget.calls + base_budget.ordinary_remaining() - 1,
            }
        )
        starved_http = _FakePagedHttp()
        with self.assertRaises(ReviewInputError):
            list(
                iter_step_down_pages(
                    http=starved_http,
                    token="t",
                    path="/repos/owner/repo/issues/7/comments",
                    deadline=30.0,
                    protect_request=starved_budget.consume,
                    hold=ReviewInputError,
                    unavailable_message="unavailable",
                    incomplete_message="incomplete",
                    deadline_message="deadline reached",
                    pagination_bound_message="pagination bound reached",
                )
            )
        self.assertEqual(len(starved_http.calls), 1)

    def test_multipart_lifecycle_preflight_and_byte_aware_receipt_paging(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            budget = EvidenceReadBudget()
            ledger = LocalSessionLedger(
                Path(directory),
                evidence_budget=budget,
                enable_finding_lifecycle_writes=True,
            )
            initialized = ledger.initialize(IDENTITY)
            op_id = session_reservation_id(
                repository="owner/repo",
                pull_request=7,
                head_sha=HEAD_SHA,
                kind="publish",
            )
            record = ledger.reserve(
                IDENTITY,
                slot="initial",
                reservation_id=op_id,
                expected_generation=initialized.generation,
                head_sha=HEAD_SHA,
            )
            payload_doc = {
                "data": "".join(random.Random(1).choices("0123456789abcdef", k=65_000))
            }
            kwargs = {
                "document": payload_doc,
                "operation_id": op_id,
                "base_sha": BASE_SHA,
                "head_sha": HEAD_SHA,
                "policy_digest": "c" * 64,
                "configuration_digest": "d" * 64,
                "item_count": 2,
            }
            calls_before = budget.calls
            plan = preflight_finding_lifecycle(ledger, record, **kwargs)
            self.assertGreaterEqual(plan["checkpoints"][0]["parts"], 2)
            # Bounded read-only preflight performed zero part writes and attached no lifecycle.
            self.assertEqual(budget.calls, calls_before + 2)
            loaded_after_preflight = ledger.load(IDENTITY).record
            assert loaded_after_preflight is not None
            self.assertIsNone(loaded_after_preflight.finding_lifecycle)

            staged = stage_finding_lifecycle(ledger, record, **kwargs)
            assert staged.finding_lifecycle is not None
            manifest = staged.finding_lifecycle["manifest"]
            self.assertEqual(manifest["encoding"], "partitioned-json-v1")
            self.assertGreaterEqual(len(manifest["parts"]), 2)
            self.assertEqual(read_finding_lifecycle(ledger, staged), payload_doc)

            # Starved budget refuses before external write.
            snap = budget.snapshot()
            starved = EvidenceReadBudget(
                snapshot={
                    **snap,
                    "calls": budget.calls + budget.ordinary_remaining() - 2,
                }
            )
            starved_ledger = LocalSessionLedger(
                Path(directory),
                evidence_budget=starved,
                enable_finding_lifecycle_writes=True,
            )
            with self.assertRaises((EvidenceBudgetExhausted, ReviewInputError)):
                preflight_finding_lifecycle(starved_ledger, record, **kwargs)

        root_comment = {
            "id": 10,
            "body": render_session_comment(
                repository_id=123, pull_request=7, record=record
            ),
        }
        heavy_comments = [{"id": 20 + idx, "body": "p" * 160_000} for idx in range(3)]
        fake_ledger = SimpleNamespace(
            _inventory_complete=True,
            _cached_comments=lambda: [root_comment, *heavy_comments],
        )
        planned = plan_plain_receipt_pages(
            fake_ledger,
            IDENTITY,
            staged_parts_count=2,
            lifecycle_parts_count=3,
        )
        self.assertIsNotNone(planned)
        assert planned is not None
        _ids, page_requests, page_bounds, total_bounds = planned
        self.assertGreater(len(page_requests), 1)
        self.assertLessEqual(len(page_requests), MAX_PAGINATION_PAGES)
        self.assertTrue(all(0 < b < MAX_GITHUB_RESPONSE_BYTES for b in page_bounds))
        self.assertEqual(total_bounds, 10)

        overflow_ledger = SimpleNamespace(
            _inventory_complete=True,
            _cached_comments=lambda: [
                root_comment,
                *[{"id": 100 + idx, "body": "x"} for idx in range(200)],
            ],
        )
        self.assertIsNone(
            plan_plain_receipt_pages(
                overflow_ledger,
                IDENTITY,
                staged_parts_count=0,
                lifecycle_parts_count=0,
            )
        )

    def test_recovery_route_advisor_readonly_classification_and_missing_config(
        self,
    ) -> None:
        acc_failed = _base_accounting(run_id=901, limit=512)
        tx_failed = ReviewTransaction(
            transaction_id=ReviewTransaction.derive_id(
                repository="owner/repo",
                pull_request=7,
                base_sha=BASE_SHA,
                head_sha=HEAD_SHA,
                policy_digest="1" * 64,
                configuration_digest="2" * 64,
                evidence_digest="3" * 64,
                reservation_id="4" * 64,
            ),
            repository="owner/repo",
            pull_request=7,
            base_sha=BASE_SHA,
            head_sha=HEAD_SHA,
            policy_digest="1" * 64,
            configuration_digest="2" * 64,
            evidence_digest="3" * 64,
            reservation_id="4" * 64,
            generation=2,
            phase="publication_failed",
            result_sha256="f" * 64,
        )
        rec_failed = _make_record(accounting=acc_failed, transaction=tx_failed)
        witness = PreExternalWriteSettlementWitness.create(
            repository="owner/repo",
            repository_id=123,
            pull_request=7,
            run_id=901,
            owner={"run_attempt": 1, "job": "hosted", "generation": 0},
            settled_root_sha256=rec_failed.record_sha256,
            settled_generation=rec_failed.generation,
            settled_phase="publication_failed",
            boundary="post_checkpoint_pre_write",
            transaction_id=tx_failed.transaction_id,
            base_sha=tx_failed.base_sha,
            head_sha=tx_failed.head_sha,
            producer="github-bot:999",
        )
        ready = classify_recovery_state(
            rec_failed,
            current_base=BASE_SHA,
            current_head="d" * 40,
            executor_status="failed",
            native_effects_confirmed=True,
            evidence_producer="github-bot:999",
            settlement_witness=witness,
        )
        self.assertFalse(ready.authority)
        self.assertTrue(ready.terminal)
        self.assertEqual(ready.state_code, "pre_external_write_settlement_ready")

        tampered = classify_recovery_state(
            rec_failed,
            current_base=BASE_SHA,
            current_head="d" * 40,
            executor_status="failed",
            native_effects_confirmed=True,
            settlement_witness={**witness.to_dict(), "unexpected": True},
        )
        self.assertNotEqual(tampered.state_code, "pre_external_write_settlement_ready")

        methods: list[tuple[str, str]] = []

        def opener(request, timeout=None):
            _ = timeout
            path = urlsplit(request.full_url).path
            methods.append((request.get_method(), path))
            if path.endswith("/issues/7/comments"):
                return json_response([])
            raise AssertionError(f"unexpected path {path}")

        ro_http = GitHubHttp(api_url="https://api.github.test", opener=opener)
        ro_ledger = GitHubIssueCommentSessionLedger(
            http=ro_http,
            token="synthetic-readonly",
            app_slug="reviewsensei[bot]",
        )
        live_res = classify_live_ledger_recovery(
            ledger=ro_ledger,
            identity=IDENTITY,
            current_base=BASE_SHA,
            current_head=HEAD_SHA,
        )
        self.assertEqual(live_res.state_code, "unenrolled_ready")
        self.assertEqual(methods, [("GET", "/repos/owner/repo/issues/7/comments")])
        self.assertIsNone(
            _inspect_pre_write_census_readonly(
                ro_ledger,
                IDENTITY,
                rec_failed,
                acc_failed,
                author_id=None,
                budget=EvidenceReadBudget(),
            )
        )


class InstalledPR491CompositionTests(unittest.TestCase):
    """Qualify PR #491 batch-scope injection, structural subdivision, and rename parity."""

    def test_multibatch_review_injects_batch_scope_and_subdivides_large_hunk(
        self,
    ) -> None:
        prod_paths = (
            "packages/python/review-sensei-github/src/review_sensei_github/action_receipt.py",
            "packages/python/review-sensei-github/src/review_sensei_github/comment_pages.py",
            "packages/python/review-sensei-github/src/review_sensei_github/fence.py",
            "packages/python/review-sensei-github/src/review_sensei_github/page_planner.py",
            "src/review_sensei/page_budgets.py",
        )
        prod_diffs = []
        for idx, path in enumerate(prod_paths):
            body_lines = "".join(
                f"+prod_{idx}_line_{line_no} = 'padding_{'x' * 34}_{line_no}'\n"
                for line_no in range(1, 79)
            )
            prod_diffs.append(
                f"diff --git a/{path} b/{path}\n"
                "--- /dev/null\n"
                f"+++ b/{path}\n"
                "@@ -0,0 +1,78 @@\n"
                f"{body_lines}"
            )
        test_path = "tests/test_byte_aware_comment_pages.py"
        test_lines = [
            f"+CONST_{line_no:03d} = 'setup_fixture_{'a' * 16}_{line_no}'\n"
            for line_no in range(1, 302)
        ]
        test_lines.append("+def _build_action_harness():\n")
        test_lines.extend(
            f"+    harness_{line_no} = 'entry_{'b' * 16}_{line_no}'\n"
            for line_no in range(303, 520)
        )
        test_lines.append("+    return (\n")
        test_lines.extend(
            f"+        ('arg_{line_no}', {'True' if line_no % 2 else 'False'}),\n"
            for line_no in range(521, 537)
        )
        test_lines.extend(["+    )\n", "+\n", "+def _preflight_and_publish():\n"])
        test_lines.extend(
            f"+    assert_page_{line_no} = 'check_{'c' * 16}_{line_no}'\n"
            for line_no in range(540, 1043)
        )
        test_diff = (
            f"diff --git a/{test_path} b/{test_path}\n"
            "--- /dev/null\n"
            f"+++ b/{test_path}\n"
            "@@ -0,0 +1,1042 @@\n" + "".join(test_lines)
        )

        class _ScopedProvider:
            name = "fixture"
            model = "fixture"

            def __init__(self) -> None:
                self.requests: list[object] = []

            def complete(self, request: object) -> ProviderResponse:
                self.requests.append(request)
                if len(self.requests) == 1:
                    summary = "Reviewed 5 production modules."
                elif len(self.requests) == 2:
                    summary = "Reviewed _build_action_harness segment."
                else:
                    summary = "Reviewed _preflight_and_publish segment."
                return ProviderResponse(
                    json.dumps({"summary": summary, "comments": []}), self.name
                )

        provider = _ScopedProvider()
        run = ReviewService(
            provider,
            budget=ResourceBudget.create(max_provider_calls=8),
            work_budgets=ReviewWorkBudgets(mode="unified"),
        ).run(
            ReviewRequest(
                diff="".join(prod_diffs) + test_diff,
                base_sha=BASE_SHA,
                head_sha=HEAD_SHA,
            )
        )
        self.assertEqual(run.outcome.status, "reviewed")
        self.assertIsNotNone(run.result)
        assert run.result is not None
        self.assertTrue(run.result.coverage.fully_reviewed)
        self.assertEqual(len(provider.requests), 3)
        for req in provider.requests:
            prompt = getattr(req, "prompt")
            self.assertIn("<review-batch-scope>", prompt)
            self.assertLessEqual(
                len((prompt + "\n\n" + _PROVIDER_OUTPUT_CORRECTION).encode("utf-8")),
                LEGACY_ASSESSMENT_PROMPT_BYTES,
            )
        self.assertIn("**Batch 1/3 (", run.result.summary)
        self.assertIn("lines 1-538):**", run.result.summary)
        self.assertIn("lines 539-1042):**", run.result.summary)

    def test_rename_hunk_reconciliation_maintains_old_and_new_path_parity(
        self,
    ) -> None:
        diff = (
            "diff --git a/legacy/old.py b/modern/new.py\n"
            "similarity index 60%\n"
            "rename from legacy/old.py\n"
            "rename to modern/new.py\n"
            "--- a/legacy/old.py\n"
            "+++ b/modern/new.py\n"
            "@@ -1 +1,2 @@\n"
            " keep1\n"
            "+add1\n"
            "@@ -10 +11,2 @@\n"
            " keep2\n"
            "+add2\n"
        )
        analysis = analyze_diff(diff)
        mixed = {
            "legacy/old.py": ("budget-exhausted", "provider-call-budget"),
            "modern/new.py": ("budget-exhausted", "provider-call-budget"),
        }
        _reconcile_file_outcomes_from_hunks(
            analysis,
            file_outcomes=mixed,
            hunk_outcomes={
                1: ("reviewed", None),
                2: ("budget-exhausted", "provider-call-budget"),
            },
        )
        self.assertEqual(
            mixed["legacy/old.py"], ("partially-reviewed", "provider-call-budget")
        )
        self.assertEqual(
            mixed["modern/new.py"], ("partially-reviewed", "provider-call-budget")
        )


class InstalledPR492AndHistoricalCompositionTests(unittest.TestCase):
    """Qualify PR #492 exact-attempt cessation, multipage pricing, G32 inventory, and replay guard."""

    def test_probe_owner_ceased_accepts_later_non_entry_and_fails_closed(self) -> None:
        base_acc = _base_accounting(run_id=1001)
        state: dict[str, object] = {
            "latest_attempt": 2,
            "jobs": {
                1: _attempt1_jobs(1001, job="hosted", conclusion="failure"),
                2: [_skipped_step_job(job_id=100102, run_id=1001, attempt=2)],
            },
        }

        def opener(request, timeout=None):
            _ = timeout
            path = urlsplit(request.full_url).path
            if path == "/repos/owner/repo/actions/runs/1001":
                return json_response(
                    {
                        "id": 1001,
                        "repository": {"id": 123, "full_name": "owner/repo"},
                        "status": "completed",
                        "conclusion": "failure",
                        "run_attempt": state["latest_attempt"],
                    }
                )
            if "/attempts/" in path and path.endswith("/jobs"):
                att = int(path.split("/attempts/")[1].split("/")[0])
                rows = copy.deepcopy(state["jobs"][att])  # type: ignore[index]
                return json_response({"total_count": len(rows), "jobs": rows})
            raise AssertionError(path)

        ledger = SimpleNamespace(
            http=GitHubHttp(api_url="https://api.github.test", opener=opener)
        )
        self.assertTrue(_probe_owner_ceased(ledger, IDENTITY, base_acc, "tok"))

        # Empty total_count == 0 or live in_progress job fails closed.
        state["jobs"][2] = []  # type: ignore[index]
        self.assertFalse(_probe_owner_ceased(ledger, IDENTITY, base_acc, "tok"))
        self.assertFalse(later_attempt_proved_no_business_entry([]))

        state["latest_attempt"] = 2 + MAX_LATER_ATTEMPT_INSPECTION
        self.assertFalse(_probe_owner_ceased(ledger, IDENTITY, base_acc, "tok"))

    def test_multipage_plain_lifecycle_and_receipt_command_pricing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            local_ledger = LocalSessionLedger(
                Path(directory),
                evidence_budget=EvidenceReadBudget(),
                enable_finding_lifecycle_writes=True,
            )
            init_rec = local_ledger.initialize(IDENTITY)
            op_id = session_reservation_id(
                repository="owner/repo",
                pull_request=7,
                head_sha=HEAD_SHA,
                kind="publish",
            )
            reserved = local_ledger.reserve(
                IDENTITY,
                slot="initial",
                reservation_id=op_id,
                expected_generation=init_rec.generation,
                head_sha=HEAD_SHA,
            )
            staged = stage_finding_lifecycle(
                local_ledger,
                reserved,
                document={
                    "data": "".join(
                        random.Random(2).choices("0123456789abcdef", k=65_000)
                    )
                },
                operation_id=op_id,
                base_sha=BASE_SHA,
                head_sha=HEAD_SHA,
                policy_digest="c" * 64,
                configuration_digest="d" * 64,
                item_count=2,
            )
            assert staged.finding_lifecycle is not None
            record = _make_record(accounting=_base_accounting(run_id=1002)).evolve(
                finding_lifecycle=staged.finding_lifecycle,
            )
            root_comment = {
                "id": 10,
                "body": render_session_comment(
                    repository_id=123, pull_request=7, record=record
                ),
            }
            heavy_comments = [
                {"id": 20 + idx, "body": "m" * 190_000} for idx in range(3)
            ]
            ledger = SimpleNamespace(
                _inventory_complete=True,
                _cached_comments=lambda: [root_comment, *heavy_comments],
            )
            receipt_pages, lifecycle_pages = _plain_multipage_counts(
                ledger,
                IDENTITY,
                current_record=record,
                action="accept-risk",
                writes_enabled=True,
                staged_parts_count=2,
            )
            self.assertGreater(receipt_pages, 1)
            self.assertGreater(lifecycle_pages, 1)

        caller = SimpleNamespace(
            accounting=SimpleNamespace(
                configuration=SimpleNamespace(limit=512),
                blocks=(),
            ),
            usable=512,
        )
        pricing = _command_pricing(
            caller,
            action="accept-risk",
            writes_enabled=True,
            finding_count=1,
            receipt_pages=receipt_pages,
            lifecycle_pages=lifecycle_pages,
        )
        self.assertEqual(
            pricing.business_mutation_requests, 10 + 2 * (lifecycle_pages - 1)
        )
        self.assertEqual(pricing.receipt_publication_requests, 6 + 2 * receipt_pages)
        status_pricing = _command_pricing(
            caller,
            action="status",
            writes_enabled=True,
            receipt_pages=receipt_pages,
            lifecycle_pages=lifecycle_pages,
        )
        self.assertEqual(status_pricing.required_requests, 1)

    def test_scoped_rollover_cessation_enforces_complete_inventory_and_deduplicates_gets(
        self,
    ) -> None:
        mode = "live_aux"
        requests: list[str] = []

        def opener(request, timeout=None):
            _ = timeout
            path = urlsplit(request.full_url).path
            requests.append(path)
            if path == "/repos/owner/repo/actions/runs/1001":
                return json_response(
                    {
                        "id": 1001,
                        "repository": {"id": 123, "full_name": "owner/repo"},
                        "status": "completed",
                        "conclusion": "failure",
                        "run_attempt": 2,
                    }
                )
            if path == "/repos/owner/repo/actions/runs/1001/attempts/1/jobs":
                rows = list(_attempt1_jobs(1001, job="hosted", conclusion="failure"))
                if mode == "live_aux":
                    rows.append(
                        {
                            "id": 100199,
                            "run_id": 1001,
                            "run_attempt": 1,
                            "name": "auxiliary-cleanup",
                            "status": "in_progress",
                            "conclusion": None,
                        }
                    )
                return json_response({"total_count": len(rows), "jobs": rows})
            if path == "/repos/owner/repo/actions/runs/1001/attempts/2/jobs":
                return json_response(
                    {
                        "total_count": 1,
                        "jobs": [
                            _skipped_step_job(job_id=100102, run_id=1001, attempt=2)
                        ],
                    }
                )
            raise AssertionError(path)

        http = GitHubHttp(api_url="https://api.github.test", opener=opener)
        ev = ApiRunActionsEvidence(
            http,
            token="synthetic-actions",
            command_run_id="457",
            job="hosted",
        )
        owner = ApiExecutionOwner(run_attempt=1, job="hosted", generation=0)

        with self.assertRaisesRegex(
            ReviewInputError, "pre-entry abort predecessor job inventory has not ceased"
        ):
            _verify_cross_run_cessation_once(
                ev,
                identity=IDENTITY,
                run_id=1001,
                owner=owner,
                require_cessation=None,
            )

        mode = "all_terminal"
        requests.clear()
        replay_calls = 0

        def require_cessation() -> None:
            nonlocal replay_calls
            replay_calls += 1
            ev.ceased_run_owner(IDENTITY, 1001, owner, complete_terminal_inventory=True)

        with _scoped_rollover_cessation(ev, require_cessation):
            _verify_cross_run_cessation_once(
                ev,
                identity=IDENTITY,
                run_id=1001,
                owner=owner,
                require_cessation=require_cessation,
            )
        self.assertEqual(replay_calls, 1)
        # Replaying captured GETs prevents a duplicate 3-GET walk (6 -> 3 requests).
        self.assertEqual(len(requests), 3)

    def test_reassessment_same_second_keys_preserve_replay_authority_guard(
        self,
    ) -> None:
        def run_case(
            *,
            source_body: str,
            with_bundle: bool,
            recorded_replay_context: str | None = None,
        ) -> tuple[str, list[dict]]:
            captured_keys: list[str] = []
            publish_calls: list[dict] = []
            decisions = (
                HumanAssessmentDecision(
                    fingerprint="f" * 64,
                    decision="dismissed",
                    rationale="Checked",
                    human_evidence="Explained",
                    diff_evidence="Verified",
                ),
            )
            returned: HumanAssessmentReply = (
                _RecordedHumanAssessmentReply(
                    body="Reassessed",
                    decisions=decisions,
                    replay_context=recorded_replay_context,
                )
                if recorded_replay_context is not None
                else HumanAssessmentReply(body="Reassessed", decisions=decisions)
            )

            def fake_through_operation(_self, **kwargs):
                captured_keys.append(str(kwargs["delivery_id"]))
                self.assertEqual(kwargs["delivery_id"], kwargs["grant"])
                return returned

            inventory = PendingHumanReview(
                base_sha=BASE_SHA,
                findings=(
                    HumanReviewFinding(
                        fingerprint="f" * 64,
                        path="src/app.py",
                        body="Confirm invariant",
                    ),
                ),
            )
            eligibility = SimpleNamespace(
                eligible=True,
                reason=None,
                human_review=inventory,
                human_inventory_reference=None,
                blocking_findings=0,
                uncertain_findings=0,
            )
            prepared = PreparedConversation(
                source_comment_id=41,
                source_updated_at="2026-10-26T00:00:00Z",
                head_sha=HEAD_SHA,
                context=ConversationContext(
                    pull_request_number=7,
                    base_sha=BASE_SHA,
                    head_sha=HEAD_SHA,
                    base_ref="main",
                ),
                root_comment_id=41,
                source_kind="issue",
            )
            human = SimpleNamespace(
                eligibility=eligibility,
                evidence_diagnostic=None,
                evidence_bundle=EvidenceReadBudget() if with_bundle else None,
                source_body=source_body,
                authority_digest="a" * 64,
                replay_context_digest="b" * 64,
                conversation=SimpleNamespace(context=prepared.context),
            )
            dummy_http = GitHubHttp(
                opener=lambda *_a, **_kw: json_response({}, status=404)
            )

            class _FakeAssessor:
                finalizer = SimpleNamespace(
                    quarantine_reader_factory=None,
                    historical_assessment_reader_factory=None,
                    qualification_authority=None,
                    http=dummy_http,
                    evidence_budget=None,
                    human_inventory_read_token=None,
                    human_inventory_reader_factory=None,
                )

                def prepare(self, **_kwargs):
                    return human

                def publish(self, **kwargs):
                    publish_calls.append(kwargs)
                    return ReplyResult(status="replied", comment_id=900)

            class _FakeConversationPublisher(ConversationPublisher):
                def __init__(self) -> None:
                    super().__init__(http=dummy_http)

                def prepare_context(self, **_kwargs):
                    return prepared

                def add_processing_reaction(self, **_kwargs):
                    return None

                def remove_processing_reaction(self, **_kwargs):
                    return None

            fake_broker = SimpleNamespace(
                request_oidc_token=lambda: "oidc",
                exchange=lambda _oidc, capability: f"tok-{capability}",
            )
            app = GitHubApplication(
                broker=fake_broker,  # type: ignore[arg-type]
                http=dummy_http,
                reviewer=ReviewPublisher(http=dummy_http),
                learner=SimpleNamespace(),  # type: ignore[arg-type]
                replier=_FakeConversationPublisher(),
            )
            with (
                patch(
                    "review_sensei_github.application.HumanAssessmentPublisher",
                    return_value=_FakeAssessor(),
                ),
                patch.object(
                    GitHubApplication,
                    "_through_operation",
                    fake_through_operation,
                ),
            ):
                app.generate_and_publish_reply(
                    options=GitHubWriteOptions(
                        github_writes=True,
                        mention_replies=True,
                        github_session_ledger=False,
                        operation_entry="enabled",
                    ),
                    oidc_token="oidc",
                    read_token="read-tok",
                    repository="owner/repo",
                    pull_request=7,
                    source_comment_id=41,
                    source_updated_at="2026-10-26T00:00:00Z",
                    expected_head_sha=HEAD_SHA,
                    reply_provider=SimpleNamespace(),  # type: ignore[arg-type]
                    model="fake",
                    app_slug="reviewsensei[bot]",
                    root_comment_id=41,
                    source_kind="issue",
                    operation_entry="enabled",
                    repository_id=None,
                    app_id=999,
                    run_id="500",
                    run_attempt="1",
                )
            return captured_keys[0], publish_calls

        for with_bundle, prefix in (
            (True, "reassessment-v2"),
            (False, "reassessment-reply-v2"),
        ):
            with self.subTest(prefix=prefix):
                key_a, calls_a = run_case(
                    source_body="@reviewsensei fixed in A", with_bundle=with_bundle
                )
                key_b, calls_b = run_case(
                    source_body="@reviewsensei fixed in B", with_bundle=with_bundle
                )
                expected_key = f"{prefix}:issue:41:2026-10-26T00:00:00Z"
                self.assertEqual(key_a, expected_key)
                self.assertEqual(key_b, expected_key)
                self.assertEqual(len(calls_a), 1)
                self.assertEqual(len(calls_b), 1)
                with self.assertRaisesRegex(
                    ReviewInputError, "human assessment replay authority changed"
                ):
                    run_case(
                        source_body="@reviewsensei fixed in B",
                        with_bundle=with_bundle,
                        recorded_replay_context="c" * 64,
                    )


def setUpModule() -> None:
    global _network
    _network = patch(
        "socket.socket.connect", side_effect=AssertionError("live network forbidden")
    )
    _network.start()


def tearDownModule() -> None:
    _network.stop()


if __name__ == "__main__":
    unittest.main()
