"""P0 tests for exact-attempt cessation, later-attempt non-entry, and advisor probe."""

from __future__ import annotations

import copy
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from fake_github_http import json_response
from review_sensei_github.api_run_host import review_startup_plan
from review_sensei_github.api_run_owner import (
    ApiRunActionsEvidence,
    _later_attempt_proved_no_business_entry,
)
from review_sensei_github.errors import GitHubPublicationError
from review_sensei_github.http import GitHubHttp
from review_sensei_github.publication import ReviewPublisher
from review_sensei_github.recovery_route_advisor import (
    _probe_executor_details_readonly,
)
from review_sensei_github.review_completion_recovery import _ReadOnlyHttp
from test_publication_failure_recovery import HEAD_2, PublicationFailureRecoveryFixture

from review_sensei.api_work import ApiExecutionOwner
from review_sensei.errors import ReviewInputError
from review_sensei.session import SessionIdentity

IDENTITY = SessionIdentity("owner/repo", 7, 123)


def _attempt1_jobs(run_id: int, *, job: str = "hosted", conclusion: str = "failure"):
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
    job_conclusion: str = "failure",
    step_name: str = "Run hosted review",
    extra_steps: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    steps: list[dict[str, object]] = [
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
    ]
    if extra_steps:
        steps.extend(extra_steps)
    return {
        "id": job_id,
        "run_id": run_id,
        "run_attempt": attempt,
        "name": f"ReviewSensei / {job}",
        "status": "completed",
        "conclusion": job_conclusion,
        "completed_at": "2026-11-22T12:05:00Z",
        "steps": steps,
    }


class P0LaterAttemptCessationTests(unittest.TestCase):
    def test_startup_plan_liabilities_stay_130_and_132(self) -> None:
        hosted_plan = review_startup_plan("hosted")
        command_plan = review_startup_plan("command")
        self.assertEqual(hosted_plan.liability, 130)
        self.assertEqual(command_plan.liability, 132)
        attempt1_slot = next(
            s for s in hosted_plan.slots if s.job == "hosted" and s.attempt == 1
        )
        self.assertEqual(len(attempt1_slot.requests), 77)

    def test_later_attempt_proved_no_business_entry_positive_and_negative_controls(
        self,
    ) -> None:
        # 1. All writer jobs skipped -> qualifies.
        self.assertTrue(
            _later_attempt_proved_no_business_entry(
                [
                    {
                        "id": 1,
                        "name": "ReviewSensei / hosted",
                        "status": "completed",
                        "conclusion": "skipped",
                    },
                    {
                        "id": 2,
                        "name": "ReviewSensei / local",
                        "status": "completed",
                        "conclusion": "skipped",
                    },
                    {
                        "id": 3,
                        "name": "ReviewSensei / command",
                        "status": "completed",
                        "conclusion": "skipped",
                    },
                ]
            )
        )

        # 2. Failed/cancelled before business step across hosted, local, and command.
        for job, step_names in (
            (
                "hosted",
                ["Run hosted review", "Generate and publish hosted mention reply"],
            ),
            ("local", ["Run local review", "Generate and publish local mention reply"]),
            ("command", ["Apply broker-attested maintainer command"]),
            ("command", ["Run command"]),
        ):
            with self.subTest(job=job, step_names=step_names):
                extra = [
                    {
                        "number": 3,
                        "name": name,
                        "status": "completed",
                        "conclusion": "skipped",
                    }
                    for name in step_names[1:]
                ]
                self.assertTrue(
                    _later_attempt_proved_no_business_entry(
                        [
                            _skipped_step_job(
                                job_id=10,
                                run_id=123,
                                attempt=2,
                                job=job,
                                job_conclusion="cancelled",
                                step_name=step_names[0],
                                extra_steps=extra,
                            )
                        ]
                    )
                )

        # 3. Negative controls: entered step, success job, empty/unrecognized steps,
        #    non-terminal step, duplicate job, or zero writer jobs.
        self.assertFalse(_later_attempt_proved_no_business_entry([]))
        self.assertFalse(
            _later_attempt_proved_no_business_entry(
                [
                    {
                        "id": 9,
                        "name": "ordinary-ci",
                        "status": "completed",
                        "conclusion": "success",
                    }
                ]
            )
        )
        # TaskAlong #62/#63 Attempt 2: Run hosted review entered and failed.
        entered_review = _skipped_step_job(
            job_id=11,
            run_id=123,
            attempt=2,
            job="hosted",
            job_conclusion="failure",
            step_name="Run hosted review",
            extra_steps=[
                {
                    "number": 3,
                    "name": "Generate and publish hosted mention reply",
                    "status": "completed",
                    "conclusion": "skipped",
                }
            ],
        )
        entered_review["steps"][1]["conclusion"] = "failure"
        self.assertFalse(_later_attempt_proved_no_business_entry([entered_review]))

        # Reply step entered even when review step skipped.
        entered_reply = _skipped_step_job(
            job_id=12,
            run_id=123,
            attempt=2,
            job="hosted",
            job_conclusion="failure",
            step_name="Run hosted review",
            extra_steps=[
                {
                    "number": 3,
                    "name": "Generate and publish hosted mention reply",
                    "status": "completed",
                    "conclusion": "failure",
                }
            ],
        )
        self.assertFalse(_later_attempt_proved_no_business_entry([entered_reply]))

        # Command step entered.
        entered_command = _skipped_step_job(
            job_id=13,
            run_id=123,
            attempt=2,
            job="command",
            job_conclusion="failure",
            step_name="Apply broker-attested maintainer command",
        )
        entered_command["steps"][1]["conclusion"] = "failure"
        self.assertFalse(_later_attempt_proved_no_business_entry([entered_command]))

        # Job conclusion == "success" even with skipped step is rejected.
        self.assertFalse(
            _later_attempt_proved_no_business_entry(
                [
                    _skipped_step_job(
                        job_id=14,
                        run_id=123,
                        attempt=2,
                        job="hosted",
                        job_conclusion="success",
                        step_name="Run hosted review",
                    )
                ]
            )
        )

        # Missing/empty/unrecognized steps fail closed.
        for bad_steps in (
            None,
            [],
            [
                {
                    "number": 1,
                    "name": "Set up Python",
                    "status": "completed",
                    "conclusion": "failure",
                }
            ],
            [
                {
                    "number": 1,
                    "name": "Run hosted review",
                    "status": "in_progress",
                    "conclusion": None,
                }
            ],
        ):
            with self.subTest(bad_steps=bad_steps):
                bad_job = {
                    "id": 15,
                    "name": "ReviewSensei / hosted",
                    "status": "completed",
                    "conclusion": "failure",
                }
                if bad_steps is not None:
                    bad_job["steps"] = bad_steps
                self.assertFalse(_later_attempt_proved_no_business_entry([bad_job]))

        # Duplicate writer kind in the same attempt is rejected.
        dup_a = _skipped_step_job(job_id=16, run_id=123, attempt=2)
        dup_b = _skipped_step_job(job_id=17, run_id=123, attempt=2)
        self.assertFalse(_later_attempt_proved_no_business_entry([dup_a, dup_b]))

    def test_ceased_run_owner_bounded_later_attempts_and_complete_inventory(
        self,
    ) -> None:
        state: dict[str, object] = {
            "latest_attempt": 5,
            "run_status": "completed",
            "run_conclusion": "failure",
            "jobs": {
                1: _attempt1_jobs(500, job="hosted", conclusion="failure"),
                2: [_skipped_step_job(job_id=502, run_id=500, attempt=2)],
                3: [
                    {
                        "id": 503,
                        "run_id": 500,
                        "run_attempt": 3,
                        "name": "ReviewSensei / hosted",
                        "status": "completed",
                        "conclusion": "skipped",
                    }
                ],
                4: [
                    _skipped_step_job(
                        job_id=504,
                        run_id=500,
                        attempt=4,
                        job="command",
                        step_name="Apply broker-attested maintainer command",
                    )
                ],
                5: [
                    _skipped_step_job(
                        job_id=505,
                        run_id=500,
                        attempt=5,
                        job="local",
                        step_name="Run local review",
                    ),
                    {
                        "id": 5059,
                        "run_id": 500,
                        "run_attempt": 5,
                        "name": "auxiliary-diagnostics",
                        "status": "completed",
                        "conclusion": "success",
                    },
                ],
            },
        }

        def opener(request, timeout=None):
            _ = timeout
            path = urlsplit(request.full_url).path
            if path == "/repos/owner/repo/actions/runs/500":
                return json_response(
                    {
                        "id": 500,
                        "run_attempt": state["latest_attempt"],
                        "status": state["run_status"],
                        "conclusion": state["run_conclusion"],
                        "repository": {"id": 123, "full_name": "owner/repo"},
                    }
                )
            if "/attempts/" in path and path.endswith("/jobs"):
                att = int(path.split("/attempts/")[1].split("/")[0])
                rows = copy.deepcopy(state["jobs"][att])
                return json_response({"total_count": len(rows), "jobs": rows})
            raise AssertionError(path)

        ev = ApiRunActionsEvidence(
            GitHubHttp(api_url="https://api.github.test", opener=opener),
            token="actions-tok",
            command_run_id="501",
            job="hosted",
        )
        # Attempt 5 (difference == 4) qualifies when all later attempts exited before entry.
        proof = ev.ceased_run_owner(
            IDENTITY,
            500,
            ApiExecutionOwner(1, "hosted", 0),
            complete_terminal_inventory=True,
        )
        self.assertEqual(proof[:4], (500, 50001, "2026-11-22T12:00:00Z", "failure"))
        self.assertEqual(len(proof[4]), 4)

        # Auxiliary job still in_progress in later attempt refuses under complete_terminal_inventory=True.
        state["jobs"][5][1]["status"] = "in_progress"
        state["jobs"][5][1]["conclusion"] = None
        with self.assertRaisesRegex(
            ReviewInputError, "later Actions attempt 5 has not ceased"
        ):
            ev.ceased_run_owner(
                IDENTITY,
                500,
                ApiExecutionOwner(1, "hosted", 0),
                complete_terminal_inventory=True,
            )
        # Under complete_terminal_inventory=False, unrelated auxiliary job does not block
        # when all business writer jobs ceased without business entry.
        ev.ceased_run_owner(
            IDENTITY,
            500,
            ApiExecutionOwner(1, "hosted", 0),
            complete_terminal_inventory=False,
        )
        state["jobs"][5][1]["status"] = "completed"
        state["jobs"][5][1]["conclusion"] = "success"

        # Exceeding bound (> 4 later attempts) refuses fail-closed.
        state["latest_attempt"] = 6
        with self.assertRaisesRegex(ReviewInputError, "too many later attempts"):
            ev.ceased_run_owner(IDENTITY, 500, ApiExecutionOwner(1, "hosted", 0))

        # Rollback conflict (run_attempt < predecessor.run_attempt) refuses.
        state["latest_attempt"] = 1
        with self.assertRaisesRegex(
            ReviewInputError, "previous Actions run attempt conflicts"
        ):
            ev.ceased_run_owner(IDENTITY, 500, ApiExecutionOwner(2, "hosted", 0))

    def test_ceased_run_owner_omitted_conclusion_allows_same_attempt_legacy_and_refuses_later_or_complete(
        self,
    ) -> None:
        state: dict[str, object] = {
            "latest_attempt": 1,
            "include_conclusion": False,
            "run_conclusion": None,
        }

        def opener(request, timeout=None):
            _ = timeout
            path = urlsplit(request.full_url).path
            if path == "/repos/owner/repo/actions/runs/600":
                payload: dict[str, object] = {
                    "id": 600,
                    "run_attempt": state["latest_attempt"],
                    "status": "completed",
                    "repository": {"id": 123, "full_name": "owner/repo"},
                    "event": "pull_request",
                }
                if state["include_conclusion"]:
                    payload["conclusion"] = state["run_conclusion"]
                return json_response(payload)
            if path == "/repos/owner/repo/actions/runs/600/attempts/1/jobs":
                rows = _attempt1_jobs(600, job="hosted", conclusion="success")
                return json_response({"total_count": len(rows), "jobs": rows})
            if path == "/repos/owner/repo/actions/runs/600/attempts/2/jobs":
                rows = [_skipped_step_job(job_id=602, run_id=600, attempt=2)]
                return json_response({"total_count": len(rows), "jobs": rows})
            raise AssertionError(path)

        ev = ApiRunActionsEvidence(
            GitHubHttp(api_url="https://api.github.test", opener=opener),
            token="actions-tok",
            command_run_id="601",
            job="hosted",
        )
        owner = ApiExecutionOwner(1, "hosted", 0)

        # (a) Same-attempt complete_terminal_inventory=False with status="completed" and
        #     omitted conclusion succeeds (matching test_media_cli_activation mock).
        proof = ev.ceased_run_owner(
            IDENTITY, 600, owner, complete_terminal_inventory=False
        )
        self.assertEqual(proof, (600, 60001, "2026-11-22T12:00:00Z", "success"))

        # (b) Later-attempt (run_attempt=2, predecessor 1) with omitted conclusion STILL refuses.
        state["latest_attempt"] = 2
        with self.assertRaisesRegex(
            ReviewInputError, "previous Actions run has not ceased"
        ):
            ev.ceased_run_owner(IDENTITY, 600, owner, complete_terminal_inventory=False)

        # (c) Same-attempt complete_terminal_inventory=True with omitted conclusion STILL refuses.
        state["latest_attempt"] = 1
        with self.assertRaisesRegex(
            ReviewInputError, "previous Actions run has not ceased"
        ):
            ev.ceased_run_owner(IDENTITY, 600, owner, complete_terminal_inventory=True)

        # (d) Same-attempt complete_terminal_inventory=False with explicit non-terminal
        #     conclusion (e.g., None/"in_progress"/"bogus") STILL refuses (G08-1).
        state["include_conclusion"] = True
        for bad_conclusion in (None, "in_progress", "bogus"):
            with self.subTest(bad_conclusion=bad_conclusion):
                state["run_conclusion"] = bad_conclusion
                with self.assertRaisesRegex(
                    ReviewInputError, "previous Actions run has not ceased"
                ):
                    ev.ceased_run_owner(
                        IDENTITY, 600, owner, complete_terminal_inventory=False
                    )

        # (e) Invalid/non-int run_attempt with omitted conclusion refuses cleanly (no TypeError).
        state["include_conclusion"] = False
        for bad_attempt in (None, "2", True, 0, -1):
            with self.subTest(bad_attempt=bad_attempt):
                state["latest_attempt"] = bad_attempt
                with self.assertRaisesRegex(
                    ReviewInputError, "previous Actions run has not ceased"
                ):
                    ev.ceased_run_owner(
                        IDENTITY, 600, owner, complete_terminal_inventory=False
                    )

    def test_end_to_end_rollover_handles_skipped_attempt2_blocks_entered_attempt2_and_detects_toctou(
        self,
    ) -> None:
        # 1. Predecessor Run 456 failed pre-write at Attempt 1 and Attempt 2 exited before
        #    "Run hosted review" -> successor Run 458 (Attempt 1, 77-call budget) rolls over!
        def fail_before_write(publisher_self, **kwargs):
            _ = publisher_self, kwargs
            raise GitHubPublicationError("synthetic pre-write crash")

        with tempfile.TemporaryDirectory() as directory:
            f = PublicationFailureRecoveryFixture(directory, limit=512)
            with patch.object(ReviewPublisher, "publish", fail_before_write):
                self.assertEqual(f.invoke(), 1)

            # Switch to new run 457 while run 456 now reports run_attempt=2 with skipped review step.
            f.next_review_run(head_sha=HEAD_2)
            prev_external = f.external

            run456_state = {
                "run_attempt": 2,
                "attempt2_entered": False,
            }

            def custom_external(request, timeout):
                path = urlsplit(request.full_url).path
                if path == "/repos/owner/repo/actions/runs/456":
                    f.trace.append((f.attempt, request.get_method(), path))
                    return json_response(
                        {
                            "id": 456,
                            "run_attempt": run456_state["run_attempt"],
                            "repository": {"id": 123, "full_name": "owner/repo"},
                            "status": "completed",
                            "conclusion": "failure",
                            "event": "pull_request",
                        }
                    )
                if path == "/repos/owner/repo/actions/runs/456/attempts/1/jobs":
                    f.trace.append((f.attempt, request.get_method(), path))
                    return json_response(
                        {
                            "total_count": 1,
                            "jobs": _attempt1_jobs(
                                456, job="hosted", conclusion="failure"
                            ),
                        }
                    )
                if path == "/repos/owner/repo/actions/runs/456/attempts/2/jobs":
                    f.trace.append((f.attempt, request.get_method(), path))
                    job = _skipped_step_job(
                        job_id=45602,
                        run_id=456,
                        attempt=2,
                        job="hosted",
                        job_conclusion="failure",
                        step_name="Run hosted review",
                        extra_steps=[
                            {
                                "number": 3,
                                "name": "Generate and publish hosted mention reply",
                                "status": "completed",
                                "conclusion": "skipped",
                            }
                        ],
                    )
                    if run456_state["attempt2_entered"]:
                        job["steps"][1]["conclusion"] = "failure"
                    return json_response({"total_count": 1, "jobs": [job]})
                return prev_external(request, timeout)

            f.external = custom_external
            # First prove negative control: Attempt 2 entered "Run hosted review" (failure) ->
            # successor Run 457 MUST hold fail-closed with zero provider calls and zero PR reviews!
            run456_state["attempt2_entered"] = True
            calls_before = f.provider_calls
            reviews_before = len(f.reviews)
            self.assertEqual(f.invoke(), 1)
            self.assertIn(
                "entered writer step with unconfirmed effects or charges", f.error
            )
            self.assertEqual(f.provider_calls, calls_before)
            self.assertEqual(len(f.reviews), reviews_before)

            # Next, Attempt 2 exited before "Run hosted review" (skipped) -> new run 458 rolls
            # over within the unchanged 77-call Attempt-1 startup budget and completes!
            run456_state["attempt2_entered"] = False
            f.next_review_run(head_sha=HEAD_2)
            self.assertEqual(f.invoke(), 0, f.error)
            self.assertGreater(len(f.reviews), reviews_before)

        # 2. TOCTOU race: Attempt 1 was observed ceased, then Attempt 2 (even skipped!)
        #    arrived before CAS -> require_cessation() detects proof tuple change and aborts!
        with tempfile.TemporaryDirectory() as directory:
            f = PublicationFailureRecoveryFixture(directory, limit=512)
            with patch.object(ReviewPublisher, "publish", fail_before_write):
                self.assertEqual(f.invoke(), 1)
            before_body = f.comment["body"]

            f.next_review_run(head_sha=HEAD_2)
            prev_external = f.external
            reads_456 = {"count": 0}

            def toctou_external(request, timeout):
                path = urlsplit(request.full_url).path
                if path == "/repos/owner/repo/actions/runs/456":
                    f.trace.append((f.attempt, request.get_method(), path))
                    reads_456["count"] += 1
                    attempt_now = 1 if reads_456["count"] == 1 else 2
                    return json_response(
                        {
                            "id": 456,
                            "run_attempt": attempt_now,
                            "repository": {"id": 123, "full_name": "owner/repo"},
                            "status": "completed",
                            "conclusion": "failure",
                            "event": "pull_request",
                        }
                    )
                if path == "/repos/owner/repo/actions/runs/456/attempts/1/jobs":
                    f.trace.append((f.attempt, request.get_method(), path))
                    return json_response(
                        {
                            "total_count": 1,
                            "jobs": _attempt1_jobs(
                                456, job="hosted", conclusion="failure"
                            ),
                        }
                    )
                if path == "/repos/owner/repo/actions/runs/456/attempts/2/jobs":
                    f.trace.append((f.attempt, request.get_method(), path))
                    return json_response(
                        {
                            "total_count": 1,
                            "jobs": [
                                _skipped_step_job(job_id=45602, run_id=456, attempt=2)
                            ],
                        }
                    )
                return prev_external(request, timeout)

            f.external = toctou_external
            calls_before = f.provider_calls
            reviews_before = len(f.reviews)
            self.assertEqual(f.invoke(), 1)
            self.assertIn("previous caller cessation changed", f.error)
            self.assertEqual(f.comment["body"], before_body)
            self.assertEqual(f.provider_calls, calls_before)
            self.assertEqual(len(f.reviews), reviews_before)

    def test_probe_executor_details_readonly_checks_run_doc_for_active_or_unconfirmed_later_attempt(
        self,
    ) -> None:
        owner = ApiExecutionOwner(1, "hosted", 0)
        state: dict[str, object] = {
            "attempt1_conclusion": "failure",
            "run_status_code": 200,
            "run_status": "in_progress",
            "run_conclusion": None,
            "run_attempt": 2,
            "attempt2_entered": False,
        }

        def opener(request, timeout=None):
            _ = timeout
            path = urlsplit(request.full_url).path
            if path == "/repos/owner/repo/actions/runs/777":
                if state["run_status_code"] != 200:
                    return json_response(
                        {"message": "missing"}, int(state["run_status_code"])
                    )
                return json_response(
                    {
                        "id": 777,
                        "repository": {"id": 123, "full_name": "owner/repo"},
                        "status": state["run_status"],
                        "conclusion": state["run_conclusion"],
                        "run_attempt": state["run_attempt"],
                        "head_sha": "a" * 40,
                        "event": "pull_request",
                    }
                )
            if path == "/repos/owner/repo/actions/runs/777/attempts/1/jobs":
                return json_response(
                    {
                        "total_count": 1,
                        "jobs": _attempt1_jobs(
                            777,
                            job="hosted",
                            conclusion=str(state["attempt1_conclusion"]),
                        ),
                    }
                )
            if path == "/repos/owner/repo/actions/runs/777/attempts/2/jobs":
                job = _skipped_step_job(job_id=77702, run_id=777, attempt=2)
                if state["attempt2_entered"]:
                    job["steps"][1]["conclusion"] = "failure"
                return json_response({"total_count": 1, "jobs": [job]})
            raise AssertionError(path)

        ro_http = _ReadOnlyHttp(
            GitHubHttp(api_url="https://api.github.test", opener=opener)
        )

        # 1. Attempt 1 failed + Attempt 2 is active `in_progress` -> `active_or_unconfirmed`.
        self.assertEqual(
            _probe_executor_details_readonly(
                ro_http,
                actions_token="tok",
                identity=IDENTITY,
                run_id=777,
                owner=owner,
                tx_head_sha="a" * 40,
            )[0],
            "active_or_unconfirmed",
        )

        # 2. Attempt 1 succeeded + Attempt 2 is active `in_progress` -> `active_or_unconfirmed`.
        state["attempt1_conclusion"] = "success"
        self.assertEqual(
            _probe_executor_details_readonly(
                ro_http,
                actions_token="tok",
                identity=IDENTITY,
                run_id=777,
                owner=owner,
                tx_head_sha="a" * 40,
            )[0],
            "active_or_unconfirmed",
        )

        # 3. Attempt 1 failed + unreadable run_doc -> `active_or_unconfirmed`.
        state["attempt1_conclusion"] = "failure"
        state["run_status_code"] = 404
        self.assertEqual(
            _probe_executor_details_readonly(
                ro_http,
                actions_token="tok",
                identity=IDENTITY,
                run_id=777,
                owner=owner,
                tx_head_sha="a" * 40,
            )[0],
            "active_or_unconfirmed",
        )

        # later_attempt_proved_no_business_entry distinguishes a writer step that exited
        # with conclusion "failure" (active_or_unconfirmed) from "skipped" (failed).
        # 4. Attempt 1 failed + Attempt 2 completed and entered writer step -> `active_or_unconfirmed`.
        state["run_status_code"] = 200
        state["run_status"] = "completed"
        state["run_conclusion"] = "failure"
        state["attempt2_entered"] = True
        self.assertEqual(
            _probe_executor_details_readonly(
                ro_http,
                actions_token="tok",
                identity=IDENTITY,
                run_id=777,
                owner=owner,
                tx_head_sha="a" * 40,
            )[0],
            "active_or_unconfirmed",
        )

        # 5. Attempt 1 failed + Attempt 2 completed with skipped writer step -> `failed`.
        state["attempt2_entered"] = False
        self.assertEqual(
            _probe_executor_details_readonly(
                ro_http,
                actions_token="tok",
                identity=IDENTITY,
                run_id=777,
                owner=owner,
                tx_head_sha="a" * 40,
            )[0],
            "failed",
        )

    def test_inspect_concurrency_posture_reuses_single_jobs_read_on_exclusive_owner(
        self,
    ) -> None:
        calls = {"runs": 0, "jobs": 0}
        jobs = [
            {
                "id": 8801,
                "run_id": 880,
                "run_attempt": 1,
                "name": "ReviewSensei / hosted",
                "status": "in_progress",
                "conclusion": None,
            },
            {
                "id": 8802,
                "run_id": 880,
                "run_attempt": 1,
                "name": "ReviewSensei / command",
                "status": "completed",
                "conclusion": "skipped",
            },
        ]

        def opener(req, timeout: float = 5.0):
            _ = timeout
            path = urlsplit(req.full_url).path
            if path == "/repos/owner/repo/actions/runs/880":
                calls["runs"] += 1
                return json_response(
                    {
                        "id": 880,
                        "run_attempt": 1,
                        "status": "in_progress",
                        "conclusion": None,
                        "event": "pull_request",
                        "repository": {"id": 123, "full_name": "owner/repo"},
                    }
                )
            if path == "/repos/owner/repo/actions/runs/880/attempts/1/jobs":
                calls["jobs"] += 1
                return json_response({"total_count": len(jobs), "jobs": jobs})
            raise AssertionError(path)

        ev = ApiRunActionsEvidence(
            GitHubHttp(api_url="https://api.github.test", opener=opener),
            token="actions-read",
            command_run_id="880",
            job="hosted",
        )
        with patch.object(
            ev,
            "current_owner",
            side_effect=AssertionError("inspect_concurrency_posture must reuse jobs"),
        ):
            posture = ev.inspect_concurrency_posture(
                IDENTITY, attempt=1, event="pull_request"
            )
        self.assertEqual(calls, {"runs": 1, "jobs": 1})
        self.assertTrue(posture.exclusive_active_owner)
        self.assertFalse(posture.overlapping_active_writers)
        self.assertEqual(posture.active_writer_jobs, ("ReviewSensei / hosted",))
        self.assertIsNone(posture.refusal_reason)


if __name__ == "__main__":
    unittest.main()
