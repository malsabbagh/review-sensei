"""A source-recognized failed review isolates targets without settling them."""

import unittest
from copy import copy, deepcopy
from dataclasses import replace

from fake_github_http import json_response, make_http
from review_sensei_github.api_run_host import review_startup_plan
from review_sensei_github.api_run_owner import ApiRunActionsEvidence

from review_sensei.api_work import (
    ApiExecutionOwner,
    ApiRunIdentity,
    FrozenApiConfiguration,
    initial_accounting,
)
from review_sensei.convergence import ReviewConvergencePolicy
from review_sensei.errors import ReviewInputError
from review_sensei.models import ReviewComment, ReviewResult, ReviewTransaction
from review_sensei.session import SessionIdentity, SessionRecord

IDENTITY = SessionIdentity("owner/repo", 7, 123)
OLD_HEAD, NEW_HEAD, BASE = "b" * 40, "c" * 40, "a" * 40
SOURCE = "86017489651cfc6ef7dd493bef255e1edebbe556"


def predecessor():
    result = ReviewResult(
        summary="Retained complete synthetic review.",
        provider="fixture",
        review_status="complete",
        comments=(
            ReviewComment(
                path="*",
                side="FILE",
                line=None,
                body="A retained assessment requiring independent original-context admission.",
            ),
        ),
    )
    transaction = replace(
        ReviewTransaction.create(
            repository=IDENTITY.repository,
            pull_request=7,
            base_sha=BASE,
            head_sha=OLD_HEAD,
            policy_digest=ReviewConvergencePolicy(mode="merge-focused").digest(),
            configuration_digest="d" * 64,
            evidence_digest="e" * 64,
            reservation_id="f" * 64,
            generation=8,
        ),
        phase="publication_failed",
        result_sha256=result.content_digest(),
    )
    result = replace(result, transaction=transaction)
    state = initial_accounting(
        ApiRunIdentity(123, 800),
        FrozenApiConfiguration("d" * 40, "d" * 64, 512),
        ApiExecutionOwner(1, "hosted", 0),
        review_startup_plan("hosted"),
        prior_nonstartup_liability_upper_bound=0,
    )
    record = SessionRecord.create(
        IDENTITY,
        generation=9,
        transaction=transaction,
        last_committed_reservation_id=transaction.reservation_id,
        completed_initial_reviews=1,
        api_run_accounting=state.to_document(),
    )
    return record, result


def run():
    return {
        "id": 800,
        "repository": {"id": 123, "full_name": "owner/repo"},
        "run_attempt": 1,
        "status": "completed",
        "conclusion": "failure",
        "head_sha": OLD_HEAD,
        "path": ".github/workflows/review-sensei-review.yml",
        "workflow_id": 700,
        "event": "pull_request",
        "updated_at": "2026-10-28T00:00:00Z",
        "referenced_workflows": [
            {
                "sha": SOURCE,
                "path": "malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@v5",
                "ref": "refs/tags/v5",
            }
        ],
    }


def jobs():
    return {
        "total_count": 6,
        "jobs": [
            {
                "id": 10 + i,
                "run_id": 800,
                "run_attempt": 1,
                "name": name,
                "status": "completed",
                "conclusion": "success",
                "completed_at": "2026-10-28T00:00:00Z",
                "steps": [],
            }
            for i, name in enumerate(
                (
                    "resolve-trigger",
                    "review-or-reply / bootstrap",
                    "review-or-reply / authoritative-preflight",
                )
            )
        ]
        + [
            {
                "id": number,
                "run_id": 800,
                "run_attempt": 1,
                "name": "review-or-reply / " + name,
                "status": "completed",
                "conclusion": "failure" if name == "hosted" else "skipped",
                "completed_at": "2026-10-28T00:00:00Z",
                "steps": [
                    {
                        "name": "Run hosted review",
                        "number": 10,
                        "status": "completed",
                        "conclusion": "failure",
                        "completed_at": "2026-10-28T00:00:00Z",
                    },
                    {
                        "name": "Generate and publish hosted mention reply",
                        "number": 11,
                        "status": "completed",
                        "conclusion": "skipped",
                    },
                ],
            }
            for number, name in enumerate(("hosted", "local", "command"), 1)
        ],
    }


def caller_sources():
    return [
        json_response(
            {
                "type": "file",
                "path": ".github/workflows/review-sensei-review.yml",
                "sha": "dbc745b3b3471cf2d9861e9a84774e34eb0d6e8b",
            }
        )
        for _ in range(2)
    ]


class QuarantineIsolationTests(unittest.TestCase):
    def authorize(
        self, values=None, *, record=None, result=None, current_head=NEW_HEAD
    ):
        from review_sensei_github.quarantine_isolation import (
            authenticate_isolated_predecessor,
        )

        old, retained = predecessor()
        http, calls = make_http(
            values
            if values is not None
            else [
                json_response(run()),
                *caller_sources(),
                json_response(jobs()),
                json_response(run()),
            ]
        )
        actions = ApiRunActionsEvidence(
            http, token="synthetic-actions", command_run_id="900"
        )
        proof = authenticate_isolated_predecessor(
            actions=actions,
            metadata_token="synthetic-metadata",
            identity=IDENTITY,
            previous=record or old,
            retained_result=result or retained,
            predecessor_comment_id=41,
            current_head=current_head,
        )
        return proof, http, calls

    def test_failed_source_targets_old_root_head_and_transaction_without_completion(
        self,
    ):
        old, result = predecessor()
        before = old.to_dict()
        proof, http, calls = self.authorize(record=old, result=result)
        proof.require_scope(
            http=http,
            identity=IDENTITY,
            previous=old,
            predecessor_comment_id=41,
            current_head=NEW_HEAD,
        )
        self.assertEqual(
            proof.receipt["targets"],
            {
                "root_comment_id": 41,
                "check_head_sha": OLD_HEAD,
                "evidence_transaction_id": old.transaction.transaction_id,
            },
        )
        self.assertEqual(proof.receipt["disposition"], "unresolved-isolated")
        self.assertEqual(old.to_dict(), before)
        self.assertIsNone(old.api_run_accounting["terminal_transaction"])
        self.assertEqual(len(calls), 5)
        self.assertTrue(all(method == "GET" for method, _, _ in calls))
        for candidate in (copy(proof),):
            with self.assertRaises(ReviewInputError):
                candidate.require_scope(
                    http=http,
                    identity=IDENTITY,
                    previous=old,
                    predecessor_comment_id=41,
                    current_head=NEW_HEAD,
                )
        with self.assertRaises(ReviewInputError):
            proof.require_scope(
                http=http,
                identity=IDENTITY,
                previous=old,
                predecessor_comment_id=42,
                current_head=NEW_HEAD,
            )

    def test_unknown_or_changed_executor_and_overlapping_writers_refuse(self):
        changed_run = deepcopy(run())
        changed_run["referenced_workflows"][0]["sha"] = "0" * 40
        changed_jobs = jobs()
        changed_jobs["jobs"][4]["conclusion"] = "success"
        later = {**run(), "run_attempt": 2}
        for values in (
            [json_response(changed_run)],
            [json_response(run()), *caller_sources(), json_response(changed_jobs)],
            [
                json_response(run()),
                *caller_sources(),
                json_response(jobs()),
                json_response(later),
            ],
        ):
            with (
                self.subTest(responses=len(values)),
                self.assertRaises(ReviewInputError),
            ):
                self.authorize(values)

    def test_unrecognized_failure_result_or_unchanged_head_refuses_before_reads(self):
        old, result = predecessor()
        for change in (
            replace(result, transaction=None, comments=()),
            replace(result, transaction=None),
        ):
            with (
                self.subTest(transaction=change.transaction is not None),
                self.assertRaises(ReviewInputError),
            ):
                self.authorize([], result=change)
        with self.assertRaises(ReviewInputError):
            self.authorize([], current_head=OLD_HEAD)
        with self.assertRaises(ReviewInputError):
            self.authorize([], record=old.evolve(operator_paused=True))

    def test_extra_unqualified_job_and_changed_caller_source_refuse(self):
        extra = jobs()
        extra["jobs"].append(
            {
                "id": 99,
                "run_id": 800,
                "run_attempt": 1,
                "name": "other-writer",
                "status": "completed",
                "conclusion": "success",
            }
        )
        extra["total_count"] += 1
        changed = {**run(), "path": ".github/workflows/foreign.yml"}
        bad_source = {
            "type": "file",
            "path": ".github/workflows/review-sensei-review.yml",
            "sha": "0" * 40,
        }
        for values in (
            [json_response(changed)],
            [json_response(run()), json_response(bad_source)],
            [json_response(run()), *caller_sources(), json_response(extra)],
        ):
            with (
                self.subTest(responses=len(values)),
                self.assertRaises(ReviewInputError),
            ):
                self.authorize(values)
