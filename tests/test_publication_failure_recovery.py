"""End-to-end tests for publication crash fixes and pre-external-write recovery (#475)."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from fake_github_http import json_response
from review_sensei_github.errors import GitHubPublicationError
from review_sensei_github.publication import ReviewPublisher
from review_sensei_github.publication_failure_recovery import (
    SETTLEMENT_WITNESS_MARKER,
    PreExternalWriteSettlementWitness,
    parse_settlement_witness_comment,
    render_settlement_witness_comment,
)
from review_sensei_github.review_run import ReviewRunHooks
from review_sensei_github.session_ledger import (
    parse_session_comment,
    render_session_comment,
)
from test_action_run import CommandEntryFixture
from test_api_work_persistence import IDENTITY

from review_sensei.errors import ReviewInputError
from review_sensei.publication_staging import PublicationStaging
from review_sensei.session import SessionRecord

HEAD_2 = "d" * 40
HEAD_3 = "e" * 40


def _witnesses_from_extra(
    extra_comments: dict[int, dict[str, object]],
) -> list[tuple[int, dict[str, object], PreExternalWriteSettlementWitness]]:
    found: list[tuple[int, dict[str, object], PreExternalWriteSettlementWitness]] = []
    for cid, row in sorted(extra_comments.items()):
        parsed = parse_settlement_witness_comment(row.get("body"))
        if parsed is not None:
            found.append((cid, row, parsed))
    return found


def _replace_finding_lifecycle(
    record: SessionRecord, lifecycle: dict[str, object]
) -> SessionRecord:
    payload = record.to_dict()
    del payload["schema_version"], payload["record_sha256"]
    payload["transaction"] = record.transaction
    payload["finding_lifecycle"] = lifecycle
    return SessionRecord._construct(**payload)


class PublicationFailureRecoveryFixture(CommandEntryFixture):
    """Command+review fixture serving head-filtered check-runs and witness writes."""

    def __init__(self, directory: str, *, limit: int = 512) -> None:
        super().__init__(directory, limit=limit)
        self.truncate_check_runs_census = False
        self.witness_post_fault: str | None = None
        self.fail_publication_failed_patch: str | None = None
        self.fail_publication_failed_patch_skip: int = 0
        self.fail_finish_patch: str | None = None
        self.graphql_response: object = None
        self.fail_review_post_status: int | None = None

    def next_review_run(self, *, head_sha: str | None = None) -> None:
        self.past_jobs[self.run_id] = self.owner_job
        self.command_active = False
        self.owner_job = "hosted"
        self.event_name = "pull_request"
        self.run_id += 1
        target_head = head_sha or self.live_head
        self.live_head = self.requested_head = target_head
        self.trace.clear()
        self.transports.clear()
        self.worker_observations.clear()

    def _meter_if_unmetered(self, request) -> None:
        if (
            len(self.accounted.observations)
            == sum(a == self.attempt for a, _, _ in self.trace) + 1
        ):
            self.trace.append(
                (self.attempt, request.get_method(), urlsplit(request.full_url).path)
            )

    def external(self, request, timeout):
        path = urlsplit(request.full_url).path
        method = request.get_method()
        payload = json.loads(request.data) if request.data else {}
        if method == "GET" and "/commits/" in path and path.endswith("/check-runs"):
            self._meter_if_unmetered(request)
            if self.truncate_check_runs_census:
                return json_response({"total_count": 5, "check_runs": []})
            head_sha = path.split("/commits/")[1].split("/")[0]
            rows = copy.deepcopy(
                [c for c in self.check_runs if c.get("head_sha") == head_sha]
            )
            return json_response({"total_count": len(rows), "check_runs": rows})
        if (
            method == "POST"
            and path.endswith("/issues/7/comments")
            and isinstance(payload, dict)
            and SETTLEMENT_WITNESS_MARKER in str(payload.get("body", ""))
        ):
            self._meter_if_unmetered(request)
            if self.witness_post_fault == "absent":
                self.witness_post_fault = None
                raise TimeoutError("synthetic lost settlement witness POST (absent)")
            cid = 3_000_000 + len(self.extra_comments) + 1
            row = {
                "id": cid,
                "body": payload["body"],
                "user": {"type": "Bot", "login": "reviewsensei[bot]", "id": 999},
                "issue_url": f"{self.api_url}/repos/owner/repo/issues/7",
            }
            self.extra_comments[cid] = copy.deepcopy(row)
            if self.witness_post_fault == "stored":
                self.witness_post_fault = None
                raise TimeoutError("synthetic lost settlement witness POST (stored)")
            return json_response(copy.deepcopy(row), 201)
        if (
            method == "PATCH"
            and "/issues/comments/" in path
            and isinstance(payload, dict)
            and isinstance(payload.get("body"), str)
        ):
            candidate = parse_session_comment(payload["body"], identity=IDENTITY)
            if (
                self.fail_publication_failed_patch
                and candidate is not None
                and candidate.transaction is not None
                and candidate.transaction.phase == "publication_failed"
            ):
                if self.fail_publication_failed_patch_skip > 0:
                    self.fail_publication_failed_patch_skip -= 1
                else:
                    fault, self.fail_publication_failed_patch = (
                        self.fail_publication_failed_patch,
                        None,
                    )
                    if fault == "stored":
                        super().external(request, timeout)
                    else:
                        self._meter_if_unmetered(request)
                    raise TimeoutError("synthetic lost publication_failed patch")
            if (
                self.fail_finish_patch
                and candidate is not None
                and candidate.transaction is not None
                and candidate.transaction.phase == "publication_succeeded"
                and candidate.api_run_accounting is not None
                and candidate.api_run_accounting.get("terminal_transaction") is not None
            ):
                fault, self.fail_finish_patch = self.fail_finish_patch, None
                if fault == "stored":
                    super().external(request, timeout)
                else:
                    self._meter_if_unmetered(request)
                raise TimeoutError("synthetic lost finish_review_run patch")
        if (
            self.graphql_response is not None
            and method == "POST"
            and path == "/graphql"
        ):
            self._meter_if_unmetered(request)
            response_obj = (
                self.graphql_response(payload)
                if callable(self.graphql_response)
                else self.graphql_response
            )
            if response_obj is not None:
                return copy.deepcopy(response_obj)
        if (
            self.fail_review_post_status is not None
            and method == "POST"
            and path.endswith("/pulls/7/reviews")
        ):
            self._meter_if_unmetered(request)
            status_code = self.fail_review_post_status
            self.fail_review_post_status = None
            return json_response(
                {"message": "synthetic review publish error"}, status_code
            )
        response = super().external(request, timeout)
        if (
            not self.command_active
            and method == "GET"
            and "/actions/runs/" in path
            and path.endswith("/jobs")
        ):
            run_id = int(path.split("/actions/runs/")[1].split("/")[0])
            if run_id in self.past_jobs:
                value = json.loads(response.read(524288))
                value["jobs"][0]["name"] = "ReviewSensei / " + self.past_jobs[run_id]
                return json_response(value)
        return response


class PublicationFailureRecoveryTests(unittest.TestCase):
    def _blocking_and_advisory_round1(
        self, directory: str, *, limit: int = 512
    ) -> PublicationFailureRecoveryFixture:
        f = PublicationFailureRecoveryFixture(directory, limit=limit)
        f.provider_comments = [
            {
                "path": "src/app.py",
                "line": 2,
                "side": "RIGHT",
                "body": "Validate caller input before updating state.",
                "severity": "high",
                "blocking": True,
                "needs_human": False,
                "symbol": "apply_change",
                "defect_kind": "missing_validation",
            },
            {
                "path": "src/app.py",
                "line": 2,
                "side": "RIGHT",
                "body": "Optional readability note on variable naming.",
                "severity": "low",
                "blocking": False,
                "needs_human": False,
                "symbol": "apply_change",
                "defect_kind": "readability",
            },
        ]
        self.assertEqual(f.invoke(), 0, f.error)
        return f

    def _round2_post_checkpoint_pre_write_failure(
        self, directory: str, *, limit: int = 1024
    ) -> PublicationFailureRecoveryFixture:
        f = self._blocking_and_advisory_round1(directory, limit=limit)
        f.next_review_run(head_sha=HEAD_2)
        prior_root = f.comment["body"]
        reviews_before = len(f.reviews)
        checks_before = len(f.check_runs)

        def fail_before_external_write(publisher_self, **kwargs):
            _ = publisher_self, kwargs
            raise GitHubPublicationError(
                "synthetic pure publication crash before write"
            )

        with patch.object(ReviewPublisher, "publish", fail_before_external_write):
            self.assertEqual(f.invoke(), 1)
        self.assertNotEqual(f.comment["body"], prior_root)
        self.assertEqual(len(f.reviews), reviews_before)
        self.assertEqual(len(f.check_runs), checks_before)
        rec = parse_session_comment(f.comment["body"], identity=IDENTITY)
        self.assertIsNotNone(rec.transaction)
        self.assertEqual(rec.transaction.phase, "publication_failed")
        self.assertIsNone(rec.reservation_id)
        self.assertFalse(f.accounted._external_business_write_attempted)
        return f

    def _assert_next_review_held(
        self,
        f: PublicationFailureRecoveryFixture,
        *,
        expected_error: str,
        head_sha: str = HEAD_3,
    ) -> None:
        saved_comment = copy.deepcopy(f.comment)
        saved_extra = copy.deepcopy(f.extra_comments)
        saved_reviews = copy.deepcopy(f.reviews)
        calls_before = f.provider_calls
        f.next_review_run(head_sha=head_sha)
        self.assertEqual(f.invoke(), 1)
        self.assertIn(expected_error, f.error)
        self.assertEqual(f.provider_calls, calls_before)
        self.assertEqual(f.comment, saved_comment)
        self.assertEqual(f.extra_comments, saved_extra)
        self.assertEqual(f.reviews, saved_reviews)
        self.assertFalse(
            any(m in {"POST", "PATCH"} and "/issues" in path for _, m, path in f.trace)
        )

    def _assert_next_command_held(
        self, f: PublicationFailureRecoveryFixture, *, expected_error: str
    ) -> None:
        saved_comment = copy.deepcopy(f.comment)
        saved_extra = copy.deepcopy(f.extra_comments)
        f.select_command("@reviewsensei review status", new_run=True)
        self.assertNotEqual(f.invoke_command(), 0)
        self.assertIn(expected_error, f.error)
        self.assertEqual(f.comment, saved_comment)
        self.assertEqual(f.extra_comments, saved_extra)
        self.assertFalse(
            any(m in {"POST", "PATCH"} and "/issues" in path for _, m, path in f.trace)
        )

    def test_475_end_to_end_advisory_promoted_to_blocking_succeeds(self):
        with tempfile.TemporaryDirectory() as directory:
            f = self._blocking_and_advisory_round1(directory, limit=512)
            r1 = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertEqual(r1.transaction.phase, "publication_succeeded")
            self.assertIsNotNone(r1.finding_lifecycle)

            # PR #475 trigger: Round 2 emits the prior advisory finding as blocking=True.
            f.next_review_run(head_sha=HEAD_2)
            f.provider_comments = [
                {
                    "path": "src/app.py",
                    "line": 2,
                    "side": "RIGHT",
                    "body": "Validate caller input before updating state.",
                    "severity": "high",
                    "blocking": True,
                    "needs_human": False,
                    "symbol": "apply_change",
                    "defect_kind": "missing_validation",
                },
                {
                    "path": "src/app.py",
                    "line": 2,
                    "side": "RIGHT",
                    "body": "Naming issue actually blocks caller clarity.",
                    "severity": "medium",
                    "blocking": True,
                    "needs_human": False,
                    "symbol": "apply_change",
                    "defect_kind": "readability",
                },
            ]
            self.assertEqual(f.invoke(), 0, f.error)
            r2 = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertEqual(r2.transaction.phase, "publication_succeeded")
            self.assertIsNotNone(r2.api_run_accounting["terminal_transaction"])
            # Prior Round 1 obligations remain retained (two unresolved items -> two reviews).
            self.assertEqual(len(f.reviews), 2)

    def test_boundary_a_pre_checkpoint_validation_settles_and_allows_command_and_next_push(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            f = self._blocking_and_advisory_round1(directory, limit=1024)
            r1_rec = parse_session_comment(f.comment["body"], identity=IDENTITY)
            r1_lifecycle = copy.deepcopy(r1_rec.finding_lifecycle)

            f.next_review_run(head_sha=HEAD_2)
            reviews_before = len(f.reviews)
            checks_before = len(f.check_runs)
            original_validate = ReviewRunHooks.validate_before_checkpoint

            def failing_validate(hook_self, **kwargs):
                _ = hook_self, kwargs
                raise ReviewInputError(
                    "synthetic pure pre-checkpoint validation refusal"
                )

            with patch.object(
                ReviewRunHooks, "validate_before_checkpoint", failing_validate
            ):
                self.assertEqual(f.invoke(), 1)
            _ = original_validate

            settled = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertIsNone(settled.transaction)
            self.assertIsNone(settled.reservation_id)
            self.assertEqual(settled.failed_attempts, 1)
            self.assertEqual(settled.failed_attempts_head_sha, HEAD_2)
            self.assertEqual(settled.finding_lifecycle, r1_lifecycle)
            self.assertFalse(f.accounted._external_business_write_attempted)
            self.assertEqual(len(f.reviews), reviews_before)
            self.assertEqual(len(f.check_runs), checks_before)

            witnesses = _witnesses_from_extra(f.extra_comments)
            self.assertEqual(len(witnesses), 1)
            w = witnesses[0][2]
            self.assertEqual(w.boundary, "pre_checkpoint_validation")
            self.assertEqual(w.settled_phase, "no-transaction")
            self.assertEqual(w.settled_root_sha256, settled.record_sha256)
            self.assertIsNone(w.transaction_id)

            # Next command succeeds and keeps Round 1 finding lifecycle manifest intact.
            f.select_command("@reviewsensei review status", new_run=True)
            self.assertEqual(f.invoke_command(), 0, f.error + f.stdout)
            after_cmd = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertIsNotNone(after_cmd.finding_lifecycle)
            self.assertEqual(
                after_cmd.finding_lifecycle["manifest"], r1_lifecycle["manifest"]
            )
            self.assertEqual(
                after_cmd.finding_lifecycle["operation_id"],
                r1_lifecycle["operation_id"],
            )

            # Next push succeeds cleanly with full obligations preserved.
            f.next_review_run(head_sha=HEAD_3)
            self.assertEqual(f.invoke(), 0, f.error)
            r3 = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertEqual(r3.transaction.phase, "publication_succeeded")
            self.assertEqual(len(f.reviews), 2)

    def test_boundary_a_round1_pre_checkpoint_failure_recovers_on_command_and_next_push(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            f = PublicationFailureRecoveryFixture(directory, limit=1024)

            def failing_validate(hook_self, **kwargs):
                _ = hook_self, kwargs
                raise ReviewInputError(
                    "synthetic round-1 pre-checkpoint validation refusal"
                )

            with patch.object(
                ReviewRunHooks, "validate_before_checkpoint", failing_validate
            ):
                self.assertEqual(f.invoke(), 1)

            settled = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertIsNone(settled.transaction)
            self.assertEqual(settled.completed_initial_reviews, 0)
            self.assertEqual(len(_witnesses_from_extra(f.extra_comments)), 1)

            f.select_command("@reviewsensei review status", new_run=True)
            self.assertEqual(f.invoke_command(), 0, f.error + f.stdout)

            f.next_review_run(head_sha=HEAD_2)
            self.assertEqual(f.invoke(), 0, f.error)
            done = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertEqual(done.transaction.phase, "publication_succeeded")

    def test_boundary_b_round2_pre_write_failure_recovers_push_same_head_and_command(
        self,
    ):
        # 1) Next push (new head)
        with tempfile.TemporaryDirectory() as directory:
            f = self._round2_post_checkpoint_pre_write_failure(directory, limit=512)
            witnesses = _witnesses_from_extra(f.extra_comments)
            self.assertEqual(len(witnesses), 1)
            self.assertEqual(witnesses[0][2].boundary, "post_checkpoint_pre_write")
            self.assertEqual(witnesses[0][2].settled_phase, "publication_failed")

            f.next_review_run(head_sha=HEAD_3)
            self.assertEqual(f.invoke(), 0, f.error)
            r3 = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertEqual(r3.transaction.phase, "publication_succeeded")
            # Round 1 finding lifecycle preserved -> second review published cleanly.
            self.assertEqual(len(f.reviews), 2)

        # 2) Same-head retry (same head_sha=HEAD_2, new run) must not falsely skip as duplicate
        with tempfile.TemporaryDirectory() as directory:
            f = self._round2_post_checkpoint_pre_write_failure(directory, limit=512)
            calls_before = f.provider_calls
            f.next_review_run(head_sha=HEAD_2)
            self.assertEqual(f.invoke(), 0, f.error)
            self.assertEqual(f.provider_calls, calls_before + 1)
            retried = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertEqual(retried.transaction.phase, "publication_succeeded")
            self.assertEqual(retried.transaction.head_sha, HEAD_2)
            self.assertEqual(len(f.reviews), 2)

        # 3) Next command (@reviewsensei review status) followed by next push
        with tempfile.TemporaryDirectory() as directory:
            f = self._round2_post_checkpoint_pre_write_failure(directory, limit=1024)
            f.select_command("@reviewsensei review status", new_run=True)
            self.assertEqual(f.invoke_command(), 0, f.error + f.stdout)
            f.next_review_run(head_sha=HEAD_3)
            self.assertEqual(f.invoke(), 0, f.error)
            self.assertEqual(len(f.reviews), 2)

    def test_boundary_b_round1_pre_write_failure_clears_speculative_initial_review_and_recovers(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            f = PublicationFailureRecoveryFixture(directory, limit=1024)

            def fail_before_external_write(publisher_self, **kwargs):
                _ = publisher_self, kwargs
                raise GitHubPublicationError("synthetic round-1 pre-write crash")

            with patch.object(ReviewPublisher, "publish", fail_before_external_write):
                self.assertEqual(f.invoke(), 1)
            self.assertEqual(len(f.reviews), 0)
            settled = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertEqual(settled.transaction.phase, "publication_failed")
            self.assertEqual(len(_witnesses_from_extra(f.extra_comments)), 1)

            f.select_command("@reviewsensei review status", new_run=True)
            self.assertEqual(f.invoke_command(), 0, f.error + f.stdout)
            self.assertEqual(len(f.reviews), 0)

            # Next push must NOT falsely trigger finding_inventory_migration_required
            # and must reset the speculative Round 1 initial-review counter back to 0 -> 1.
            f.next_review_run(head_sha=HEAD_2)
            self.assertEqual(f.invoke(), 0, f.error)
            r2 = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertEqual(r2.transaction.phase, "publication_succeeded")
            self.assertEqual(r2.completed_initial_reviews, 1)
            self.assertGreater(len(f.reviews), 0)
            self.assertTrue(all(r["commit_id"] == HEAD_2 for r in f.reviews))

    def test_fail_closed_negative_controls_block_rollover_without_writes(self):
        # 1) Missing witness comment (missing receipt is never proof of no write)
        with tempfile.TemporaryDirectory() as directory:
            f = self._round2_post_checkpoint_pre_write_failure(directory)
            f.extra_comments.clear()
            self._assert_next_review_held(
                f, expected_error="missing pre-external-write settlement witness"
            )

        # 2) Tampered witness (field / content_sha256 mismatch)
        with tempfile.TemporaryDirectory() as directory:
            f = self._round2_post_checkpoint_pre_write_failure(directory)
            cid, _, w = _witnesses_from_extra(f.extra_comments)[0]
            tampered = w.to_dict()
            tampered["settled_root_sha256"] = "f" * 64
            encoded = json.dumps(
                tampered, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            )
            f.extra_comments[cid]["body"] = (
                "ReviewSensei recorded a pre-external-write failure settlement witness "
                "(data only; requires independent Actions cessation and native negative census).\n\n"
                f"```json\n{encoded}\n```\n"
                f"{SETTLEMENT_WITNESS_MARKER}"
            )
            self._assert_next_review_held(
                f, expected_error="tampered or corrupt bot settlement witness"
            )

        # 3) Foreign-authored witness (user.id != 999 / non-Bot)
        with tempfile.TemporaryDirectory() as directory:
            f = self._round2_post_checkpoint_pre_write_failure(directory)
            cid, _, _ = _witnesses_from_extra(f.extra_comments)[0]
            f.extra_comments[cid]["user"] = {
                "type": "User",
                "login": "attacker",
                "id": 888,
            }
            self._assert_next_review_held(
                f, expected_error="missing pre-external-write settlement witness"
            )

        # 4) Unexplained review on failed head_sha
        with tempfile.TemporaryDirectory() as directory:
            f = self._round2_post_checkpoint_pre_write_failure(directory)
            f.reviews.append(
                {
                    "id": 777,
                    "commit_id": HEAD_2,
                    "state": "COMMENTED",
                    "body": (
                        "late partial review\n\n"
                        f"<!-- reviewsensei:review:v1 repo=123 pr=7 head={HEAD_2} "
                        f"result={'a' * 64} coverage=full -->"
                    ),
                    "user": {"type": "Bot", "login": "reviewsensei[bot]", "id": 999},
                }
            )
            self._assert_next_review_held(
                f, expected_error="late native effect on the failed scope"
            )

        # 5) Unexplained inline comment on failed head_sha
        with tempfile.TemporaryDirectory() as directory:
            f = self._round2_post_checkpoint_pre_write_failure(directory)
            f.inline.append(
                {
                    "id": 888,
                    "pull_request_review_id": 777,
                    "commit_id": HEAD_2,
                    "original_commit_id": HEAD_2,
                    "body": (
                        "late partial inline comment\n\n"
                        f"<!-- reviewsensei:finding:v2 repo=123 pr=7 head={HEAD_2} "
                        f"base={'a' * 40} fingerprint={'b' * 64} state=new blocking=true -->"
                    ),
                    "user": {"type": "Bot", "login": "reviewsensei[bot]", "id": 999},
                }
            )
            self._assert_next_review_held(
                f, expected_error="late native effect on the failed scope"
            )

        # 6) Unexplained ReviewSensei check-run on failed head_sha
        with tempfile.TemporaryDirectory() as directory:
            f = self._round2_post_checkpoint_pre_write_failure(directory)
            f.check_runs.append(
                {
                    "id": 9991,
                    "name": "ReviewSensei",
                    "external_id": "reviewsensei-review-v1",
                    "head_sha": HEAD_2,
                    "status": "in_progress",
                    "app": {"id": 999, "slug": "reviewsensei"},
                }
            )
            self._assert_next_review_held(
                f, expected_error="late native check-run effect on the failed scope"
            )

        # 7) External business write started -> witness is NOT emitted -> next run holds
        with tempfile.TemporaryDirectory() as directory:
            f = self._blocking_and_advisory_round1(directory, limit=1024)
            f.next_review_run(head_sha=HEAD_2)

            def start_write_then_fail(publisher_self, **kwargs):
                _ = kwargs
                publisher_self.http.request(
                    "POST",
                    "/repos/owner/repo/check-runs",
                    token="synthetic",
                    body={"name": "ReviewSensei", "head_sha": HEAD_2},
                )
                raise GitHubPublicationError(
                    "synthetic failure after external write started"
                )

            with patch.object(ReviewPublisher, "publish", start_write_then_fail):
                self.assertEqual(f.invoke(), 1)
            self.assertTrue(f.accounted._external_business_write_attempted)
            self.assertEqual(len(_witnesses_from_extra(f.extra_comments)), 0)
            self._assert_next_review_held(
                f, expected_error="missing pre-external-write settlement witness"
            )

        # 8) Active predecessor Actions run / incomplete job inventory -> refuses rollover
        with tempfile.TemporaryDirectory() as directory:
            f = self._round2_post_checkpoint_pre_write_failure(directory)
            f.previous_active = True
            self._assert_next_review_held(f, expected_error="has not ceased")

        with tempfile.TemporaryDirectory() as directory:
            f = self._round2_post_checkpoint_pre_write_failure(directory)
            f.previous_auxiliary_running = True
            self._assert_next_review_held(
                f, expected_error="predecessor job inventory has not ceased"
            )

        # 9) Truncated census pagination -> refuses rollover
        with tempfile.TemporaryDirectory() as directory:
            f = self._round2_post_checkpoint_pre_write_failure(directory)
            f.truncate_check_runs_census = True
            self._assert_next_review_held(
                f, expected_error="native check-run pagination is incomplete"
            )

        # 10) finding_lifecycle.operation_id == tx.transaction_id OR publication_staging present
        with tempfile.TemporaryDirectory() as directory:
            f = self._round2_post_checkpoint_pre_write_failure(directory)
            rec = parse_session_comment(f.comment["body"], identity=IDENTITY)
            tx = rec.transaction
            mutated_lc = copy.deepcopy(rec.finding_lifecycle)
            mutated_lc["operation_id"] = tx.transaction_id
            raw_payload = rec.to_dict()
            del raw_payload["schema_version"], raw_payload["record_sha256"]
            raw_payload["transaction"] = tx
            raw_payload["finding_lifecycle"] = mutated_lc
            bad_rec = SessionRecord._construct(**raw_payload)
            f.comment["body"] = render_session_comment(
                repository_id=123, pull_request=7, record=bad_rec
            )
            # Re-bind witness root to bad_rec so admission specifically trips the lifecycle check
            cid, _, w = _witnesses_from_extra(f.extra_comments)[0]
            updated_w = PreExternalWriteSettlementWitness.create(
                repository=w.repository,
                repository_id=w.repository_id,
                pull_request=w.pull_request,
                run_id=w.run_id,
                owner=w.owner,
                settled_root_sha256=bad_rec.record_sha256,
                settled_generation=bad_rec.generation,
                settled_phase=w.settled_phase,
                boundary=w.boundary,
                transaction_id=w.transaction_id,
                base_sha=w.base_sha,
                head_sha=w.head_sha,
                producer=w.producer,
            )
            f.extra_comments[cid]["body"] = render_settlement_witness_comment(updated_w)
            self._assert_next_review_held(
                f,
                expected_error="finding lifecycle already recorded effects for the failed transaction",
            )

        with tempfile.TemporaryDirectory() as directory:
            f = self._round2_post_checkpoint_pre_write_failure(directory)
            rec = parse_session_comment(f.comment["body"], identity=IDENTITY)
            tx = rec.transaction
            staging = PublicationStaging(
                transaction_id=tx.transaction_id,
                result_sha256=tx.result_sha256,
                plan_sha256="a" * 64,
                part_count=1,
            ).to_document()
            bad_rec = rec.evolve(publication_staging=staging)
            f.comment["body"] = render_session_comment(
                repository_id=123, pull_request=7, record=bad_rec
            )
            self._assert_next_review_held(
                f, expected_error="does not qualify for pre-write failure rollover"
            )

    def test_lost_close_response_matrix(self):
        # 1a) Lost mark_review_publication_unconfirmed PATCH: stored -> reconciled (old.uncertain == 1) -> witness emitted -> next push succeeds
        with tempfile.TemporaryDirectory() as directory:
            f = self._blocking_and_advisory_round1(directory, limit=1024)
            f.next_review_run(head_sha=HEAD_2)
            f.fail_publication_failed_patch = "stored"

            def fail_pub(publisher_self, **kwargs):
                _ = publisher_self, kwargs
                raise GitHubPublicationError("synthetic pre-write failure")

            with patch.object(ReviewPublisher, "publish", fail_pub):
                self.assertEqual(f.invoke(), 1)
            rec = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertEqual(rec.transaction.phase, "publication_failed")
            self.assertEqual(len(_witnesses_from_extra(f.extra_comments)), 1)
            f.next_review_run(head_sha=HEAD_3)
            self.assertEqual(f.invoke(), 0, f.error)

        # 1b) Lost mark_review_publication_unconfirmed PATCH: absent -> stays publication_pending without witness -> next push holds
        with tempfile.TemporaryDirectory() as directory:
            f = self._blocking_and_advisory_round1(directory, limit=1024)
            f.next_review_run(head_sha=HEAD_2)
            f.fail_publication_failed_patch = "absent"

            def fail_pub(publisher_self, **kwargs):
                _ = publisher_self, kwargs
                raise GitHubPublicationError("synthetic pre-write failure")

            with patch.object(ReviewPublisher, "publish", fail_pub):
                self.assertEqual(f.invoke(), 1)
            rec = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertEqual(rec.transaction.phase, "publication_pending")
            self.assertEqual(len(_witnesses_from_extra(f.extra_comments)), 0)
            self._assert_next_review_held(f, expected_error="phase=publication_pending")

        # 1c) Lost complete_review_publication(..., published=False) PATCH: stored -> reconciled -> witness emitted -> next push succeeds
        with tempfile.TemporaryDirectory() as directory:
            f = self._blocking_and_advisory_round1(directory, limit=1024)
            f.next_review_run(head_sha=HEAD_2)
            f.fail_publication_failed_patch_skip = 1
            f.fail_publication_failed_patch = "stored"

            def fail_pub(publisher_self, **kwargs):
                _ = publisher_self, kwargs
                raise GitHubPublicationError("synthetic pre-write failure")

            with patch.object(ReviewPublisher, "publish", fail_pub):
                self.assertEqual(f.invoke(), 1)
            rec = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertEqual(rec.transaction.phase, "publication_failed")
            self.assertEqual(len(_witnesses_from_extra(f.extra_comments)), 1)
            f.next_review_run(head_sha=HEAD_3)
            self.assertEqual(f.invoke(), 0, f.error)

        # 1d) Lost complete_review_publication(..., published=False) PATCH: absent -> stays publication_failed without witness -> next push holds
        with tempfile.TemporaryDirectory() as directory:
            f = self._blocking_and_advisory_round1(directory, limit=1024)
            f.next_review_run(head_sha=HEAD_2)
            f.fail_publication_failed_patch_skip = 1
            f.fail_publication_failed_patch = "absent"

            def fail_pub(publisher_self, **kwargs):
                _ = publisher_self, kwargs
                raise GitHubPublicationError("synthetic pre-write failure")

            with patch.object(ReviewPublisher, "publish", fail_pub):
                self.assertEqual(f.invoke(), 1)
            rec = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertEqual(rec.transaction.phase, "publication_failed")
            self.assertEqual(len(_witnesses_from_extra(f.extra_comments)), 0)
            self._assert_next_review_held(
                f, expected_error="missing pre-external-write settlement witness"
            )

        # 2a) Lost witness POST response: stored -> next push AND next command succeed
        with tempfile.TemporaryDirectory() as directory:
            f = self._blocking_and_advisory_round1(directory, limit=1024)
            f.next_review_run(head_sha=HEAD_2)
            f.witness_post_fault = "stored"

            def fail_pub(publisher_self, **kwargs):
                _ = publisher_self, kwargs
                raise GitHubPublicationError("synthetic pre-write failure")

            with patch.object(ReviewPublisher, "publish", fail_pub):
                self.assertEqual(f.invoke(), 1)
            self.assertEqual(len(_witnesses_from_extra(f.extra_comments)), 1)
            f.select_command("@reviewsensei review status", new_run=True)
            self.assertEqual(f.invoke_command(), 0, f.error + f.stdout)
            f.next_review_run(head_sha=HEAD_3)
            self.assertEqual(f.invoke(), 0, f.error)

        # 2b) Lost witness POST response: absent -> reconciles cleanly when retained result parts
        #     and clean 3-surface census exist, and still holds fail-closed if retained parts
        #     are missing or a native effect is present.
        with tempfile.TemporaryDirectory() as directory:
            f = self._blocking_and_advisory_round1(directory, limit=1024)
            f.next_review_run(head_sha=HEAD_2)
            f.witness_post_fault = "absent"

            def fail_pub(publisher_self, **kwargs):
                _ = publisher_self, kwargs
                raise GitHubPublicationError("synthetic pre-write failure")

            with patch.object(ReviewPublisher, "publish", fail_pub):
                self.assertEqual(f.invoke(), 1)
            self.assertEqual(len(_witnesses_from_extra(f.extra_comments)), 0)
            f.select_command("@reviewsensei review status", new_run=True)
            self.assertEqual(f.invoke_command(), 0, f.error + f.stdout)
            f.next_review_run(head_sha=HEAD_3)
            self.assertEqual(f.invoke(), 0, f.error)

        with tempfile.TemporaryDirectory() as directory:
            f = self._blocking_and_advisory_round1(directory, limit=1024)
            f.next_review_run(head_sha=HEAD_2)
            f.witness_post_fault = "absent"

            def fail_pub(publisher_self, **kwargs):
                _ = publisher_self, kwargs
                raise GitHubPublicationError("synthetic pre-write failure")

            with patch.object(ReviewPublisher, "publish", fail_pub):
                self.assertEqual(f.invoke(), 1)
            self.assertEqual(len(_witnesses_from_extra(f.extra_comments)), 0)
            f.extra_comments.clear()
            self._assert_next_command_held(
                f, expected_error="missing pre-external-write settlement witness"
            )
            self._assert_next_review_held(
                f, expected_error="missing pre-external-write settlement witness"
            )

        # 3a) Lost finish_review_run response after normal publication: stored -> reconciled cleanly (exit 0) -> next push rolls over
        with tempfile.TemporaryDirectory() as directory:
            f = PublicationFailureRecoveryFixture(directory, limit=512)
            f.fail_finish_patch = "stored"
            self.assertEqual(f.invoke(), 0, f.error)
            rec = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertEqual(rec.transaction.phase, "publication_succeeded")
            self.assertEqual(
                rec.api_run_accounting["terminal_transaction"],
                rec.transaction.transaction_id,
            )
            f.next_review_run(head_sha=HEAD_2)
            self.assertEqual(f.invoke(), 0, f.error)
            recovered = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertEqual(recovered.transaction.phase, "publication_succeeded")

        # 3b) Lost finish_review_run response after normal publication: absent -> exit 1 without terminal close -> next push fails closed when executor did not exit cleanly
        with tempfile.TemporaryDirectory() as directory:
            f = PublicationFailureRecoveryFixture(directory, limit=512)
            f.fail_finish_patch = "absent"
            self.assertEqual(f.invoke(), 1)
            rec = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertEqual(rec.transaction.phase, "publication_succeeded")
            self.assertIsNone(rec.api_run_accounting["terminal_transaction"])
            f.previous_conclusion = "failure"
            self._assert_next_review_held(
                f,
                expected_error="historical executor did not prove successful review exit",
                head_sha=HEAD_2,
            )

        # 4a) Direct abort-close-v1 lost close response: stored -> next command AND next push succeed
        with tempfile.TemporaryDirectory() as directory:
            f = PublicationFailureRecoveryFixture(directory, limit=1024)

            def abort_before_entry():
                raise KeyboardInterrupt()

            f.before_entry = abort_before_entry
            f.abort_fault = "closure-stored"
            self.assertEqual(f.invoke(), 130)
            rec = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertIsNotNone(rec.api_run_accounting["analysis_abort"])
            f.before_entry = None

            f.select_command("@reviewsensei review status", new_run=True)
            self.assertEqual(f.invoke_command(), 0, f.error + f.stdout)
            f.next_review_run(head_sha=HEAD_2)
            self.assertEqual(f.invoke(), 0, f.error)

        # 4b) Direct abort-close-v1 lost close response: absent -> next command AND next push hold
        with tempfile.TemporaryDirectory() as directory:
            f = PublicationFailureRecoveryFixture(directory, limit=1024)

            def abort_before_entry():
                raise KeyboardInterrupt()

            f.before_entry = abort_before_entry
            f.abort_fault = "closure-absent"
            self.assertEqual(f.invoke(), 130)
            rec = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertIsNone(rec.api_run_accounting["analysis_abort"])
            f.before_entry = None

            self._assert_next_command_held(f, expected_error="phase=no-transaction")
            self._assert_next_review_held(f, expected_error="phase=no-transaction")

    def test_bug1_pre_write_graphql_failure_after_journal_plan_and_post_effect_contrast(
        self,
    ):
        # 1) Pre-write failure AFTER lifecycle_journal.plan() inside _published_finding_suppression
        #    (profile#130 pattern) must NOT checkpoint finding_lifecycle with operation_id == tx_id,
        #    must emit post_checkpoint_pre_write witness, and must allow subsequent recovery.
        with tempfile.TemporaryDirectory() as directory:
            f = self._blocking_and_advisory_round1(directory, limit=512)
            r1 = parse_session_comment(f.comment["body"], identity=IDENTITY)
            r1_op_id = r1.finding_lifecycle["operation_id"]

            f.next_review_run(head_sha=HEAD_2)
            reviews_before = len(f.reviews)
            checks_before = len(f.check_runs)
            f.graphql_response = json_response({"errors": [{"message": "not exposed"}]})
            self.assertEqual(f.invoke(), 1)
            self.assertIn("graphql unknown_error", f.error)
            self.assertEqual(len(f.reviews), reviews_before)
            self.assertEqual(len(f.check_runs), checks_before)
            self.assertFalse(f.accounted._external_business_write_attempted)

            settled = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertEqual(settled.transaction.phase, "publication_failed")
            self.assertIsNotNone(settled.finding_lifecycle)
            self.assertEqual(settled.finding_lifecycle["operation_id"], r1_op_id)
            self.assertNotEqual(
                settled.finding_lifecycle["operation_id"],
                settled.transaction.transaction_id,
            )
            witnesses = _witnesses_from_extra(f.extra_comments)
            self.assertEqual(len(witnesses), 1)
            self.assertEqual(witnesses[0][2].boundary, "post_checkpoint_pre_write")

            f.graphql_response = None
            f.next_review_run(head_sha=HEAD_3)
            self.assertEqual(f.invoke(), 0, f.error)
            r3 = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertEqual(r3.transaction.phase, "publication_succeeded")
            self.assertEqual(len(f.reviews), 2)

        # 2) Contrast: once a native effect (check-run POST) AND an unconfirmed
        #    review POST (recorded as status='pending') exist, fail-closed holds;
        #    whereas confirmed-only check-run(s) with 0 pending effects (Path A)
        #    reconcile cleanly on the next run.
        with tempfile.TemporaryDirectory() as directory:
            f = self._blocking_and_advisory_round1(directory, limit=1024)
            f.next_review_run(head_sha=HEAD_2)
            f.fail_review_post_status = 422
            self.assertEqual(f.invoke(), 1)
            settled = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertEqual(settled.transaction.phase, "publication_failed")
            self.assertEqual(
                settled.finding_lifecycle["operation_id"],
                settled.transaction.transaction_id,
            )
            self.assertEqual(len(_witnesses_from_extra(f.extra_comments)), 0)
            self._assert_next_review_held(
                f,
                expected_error="finding lifecycle",
                head_sha=HEAD_3,
            )

        with tempfile.TemporaryDirectory() as directory:
            f = self._blocking_and_advisory_round1(directory, limit=1024)
            f.next_review_run(head_sha=HEAD_2)
            checks_before = len(f.check_runs)

            def fail_after_check_run(pub_self, **_kwargs):
                _ = pub_self, _kwargs
                raise GitHubPublicationError("drift preflight failed after check-run")

            with patch.object(
                ReviewPublisher, "_update_existing_drift_threads", fail_after_check_run
            ):
                self.assertEqual(f.invoke(), 1)
            self.assertGreater(len(f.check_runs), checks_before)
            settled = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertEqual(settled.transaction.phase, "publication_failed")
            self.assertEqual(
                settled.finding_lifecycle["operation_id"],
                settled.transaction.transaction_id,
            )
            self.assertEqual(len(_witnesses_from_extra(f.extra_comments)), 0)
            f.next_review_run(head_sha=HEAD_3)
            self.assertEqual(f.invoke(), 0, f.error)
            r3 = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertEqual(r3.transaction.phase, "publication_succeeded")

    def test_bug2_round2_pre_write_recovery_restores_baseline_and_round_counters(self):
        with tempfile.TemporaryDirectory() as directory:
            f = PublicationFailureRecoveryFixture(directory, limit=512)
            f.provider_comments = [
                {
                    "path": "src/app.py",
                    "line": 2,
                    "side": "RIGHT",
                    "body": "Validate caller input before updating state.",
                    "severity": "high",
                    "blocking": True,
                    "needs_human": False,
                    "symbol": "apply_change",
                    "defect_kind": "missing_validation",
                }
            ]
            self.assertEqual(f.invoke(), 0, f.error)
            r1 = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertEqual(r1.completed_initial_reviews, 1)
            self.assertEqual(r1.completed_verification_rounds, 0)
            r1_history = copy.deepcopy(r1.convergence_history)
            self.assertEqual(
                r1_history["baseline"]["cache_key"]["head_sha"],
                f.live_head,
            )

            # Direct unit contract: SessionRecord.rollover_pre_write_settled_api_accounting
            # rejects missing or mismatched prior_record when previous_record exists.
            f.next_review_run(head_sha=HEAD_2)

            def fail_before_external_write(publisher_self, **kwargs):
                _ = publisher_self, kwargs
                raise GitHubPublicationError("synthetic round-2 pre-write crash")

            with patch.object(ReviewPublisher, "publish", fail_before_external_write):
                self.assertEqual(f.invoke(), 1)

            settled = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertEqual(settled.transaction.phase, "publication_failed")
            self.assertEqual(settled.completed_initial_reviews, 1)
            self.assertEqual(settled.completed_verification_rounds, 1)
            self.assertEqual(
                settled.convergence_history["baseline"]["cache_key"]["head_sha"],
                HEAD_2,
            )

            dummy_next_accounting = {
                **settled.api_run_accounting,
                "identity": {
                    **settled.api_run_accounting["identity"],
                    "run_id": settled.api_run_accounting["identity"]["run_id"] + 1,
                },
                "owner": {
                    "run_attempt": 1,
                    "job": "hosted",
                    "generation": 0,
                },
                "confirmed": [],
                "uncertain": [],
                "prior_nonstartup_liability": 0,
                "startup": {
                    "slots": [
                        {
                            "attempt": 1,
                            "job": "hosted",
                            "requests": ["repository-policy"],
                        }
                    ]
                },
                "blocks": [],
                "previous_record": {
                    **settled.api_run_accounting["previous_record"],
                    "run_id": settled.api_run_accounting["identity"]["run_id"],
                    "root_sha256": settled.record_sha256,
                },
                "action": None,
                "analysis_abort": None,
                "terminal_transaction": None,
            }
            with self.assertRaises(ReviewInputError):
                settled.rollover_pre_write_settled_api_accounting(dummy_next_accounting)
            with self.assertRaises(ReviewInputError):
                settled.rollover_pre_write_settled_api_accounting(
                    dummy_next_accounting, prior_record=settled
                )

            # Recovery command restores Round 1's baseline and round counters without
            # burning a verification round for the unpublished Round 2 failure,
            # reads previous_record once, and leaves no leaked prior_record handoff.
            from review_sensei_github import publication_failure_recovery as pfr

            f.select_command("@reviewsensei review status", new_run=True)
            with patch.object(
                pfr, "read_previous_run", wraps=pfr.read_previous_run
            ) as mock_read_prev:
                self.assertEqual(f.invoke_command(), 0, f.error + f.stdout)
            self.assertEqual(mock_read_prev.call_count, 1)
            self.assertIsNone(
                pfr.consume_admitted_prior_record(f.selected.ledger, settled)
            )
            restored = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertIsNone(restored.transaction)
            self.assertEqual(restored.completed_initial_reviews, 1)
            self.assertEqual(restored.completed_verification_rounds, 0)
            self.assertEqual(
                restored.convergence_history["baseline"], r1_history["baseline"]
            )
            self.assertEqual(
                restored.convergence_history["progress"], r1_history["progress"]
            )

            # Subsequent push uses the restored Round 1 baseline and completes with
            # verification round count 1 (not 2).
            f.next_review_run(head_sha=HEAD_3)
            self.assertEqual(f.invoke(), 0, f.error)
            r3 = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertEqual(r3.transaction.phase, "publication_succeeded")
            self.assertEqual(r3.completed_initial_reviews, 1)
            self.assertEqual(r3.completed_verification_rounds, 1)
            self.assertEqual(
                r3.convergence_history["baseline"]["cache_key"]["head_sha"],
                HEAD_3,
            )

    def test_bug4a_dictionary_packed_settled_ancestor_explains_same_head_effects(self):
        from review_sensei_github import finding_lifecycle_dictionary as dict_codec
        from review_sensei_github.finding_lifecycle import _unpack_journal, _validate
        from review_sensei_github.http import GitHubHttp
        from review_sensei_github.publication_failure_recovery import (
            _NO_EARLIER_EFFECTS,
            _earlier_settled_effects,
        )
        from review_sensei_github.session_ledger import GitHubIssueCommentSessionLedger

        from review_sensei.api_work import ApiRunAccounting
        from review_sensei.bounded_evidence import (
            EvidenceReadBudget,
            canonical_bytes,
            evidence_manifest,
            inline_manifest,
            partition_evidence,
            read_inline_evidence,
        )

        with tempfile.TemporaryDirectory() as directory:
            f = self._blocking_and_advisory_round1(directory, limit=1024)
            same_head = f.live_head
            r1 = parse_session_comment(f.comment["body"], identity=IDENTITY)
            orig_ref = r1.finding_lifecycle
            orig_manifest = orig_ref["manifest"]
            orig_binding = orig_manifest["binding"]
            orig_envelope = read_inline_evidence(
                orig_manifest, expected_binding=orig_binding
            )
            journal_doc = _validate(_unpack_journal(orig_envelope["lifecycle"]))

            # Part 1: end-to-end real partitioned-json-v1 same-head pre-write recovery.
            base_parts = partition_evidence(
                orig_envelope, binding=orig_binding, item_count=1
            )
            base_storage_ids: list[str] = []
            for idx, part in enumerate(base_parts):
                cid = 2_000_100 + idx
                f.extra_comments[cid] = {
                    "id": cid,
                    "issue_url": f"{f.api_url}/repos/owner/repo/issues/7",
                    "body": (
                        "ReviewSensei immutable evidence part v1\n```json\n"
                        + canonical_bytes(part).decode("utf-8")
                        + "\n```\n<!-- reviewsensei:evidence-part:v1 -->"
                    ),
                    "user": {
                        "type": "Bot",
                        "login": "reviewsensei[bot]",
                        "id": 999,
                    },
                }
                base_storage_ids.append(str(cid))
            base_part_manifest = evidence_manifest(
                orig_envelope,
                binding=orig_binding,
                item_count=1,
                parts=base_parts,
                storage_ids=tuple(base_storage_ids),
            )
            r1_part = _replace_finding_lifecycle(
                r1, {**orig_ref, "manifest": base_part_manifest}
            )
            f.comment["body"] = render_session_comment(
                repository_id=123, pull_request=7, record=r1_part
            )

            f.next_review_run(head_sha=HEAD_2)

            def fail_before_external_write(publisher_self, **kwargs):
                _ = publisher_self, kwargs
                raise GitHubPublicationError("synthetic pre-write failure")

            with patch.object(ReviewPublisher, "publish", fail_before_external_write):
                self.assertEqual(f.invoke(), 1)
            self.assertEqual(len(_witnesses_from_extra(f.extra_comments)), 1)
            settled_part = parse_session_comment(f.comment["body"], identity=IDENTITY)

            f.select_command("@reviewsensei review status", new_run=True)
            self.assertEqual(f.invoke_command(), 0, f.error + f.stdout)
            after = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertIsNone(after.transaction)

            # Part 2: direct _earlier_settled_effects verification for defensive
            # finding-lifecycle-packed-v4 referencing the partitioned base.
            dict_payload = dict_codec.pack(
                journal_doc,
                base=orig_envelope,
                manifest=base_part_manifest,
                current_binding=orig_binding,
            )
            dict_envelope = {
                "schema_version": "finding-lifecycle-evidence-v2",
                "lifecycle": dict_payload,
            }
            dict_manifest = inline_manifest(
                dict_envelope, binding=orig_binding, item_count=1
            )
            r1_dict = _replace_finding_lifecycle(
                r1_part, {**orig_ref, "manifest": dict_manifest}
            )
            settled_payload = settled_part.to_dict()
            del settled_payload["schema_version"], settled_payload["record_sha256"]
            settled_payload["transaction"] = settled_part.transaction
            settled_payload["finding_lifecycle"] = r1_dict.finding_lifecycle
            accounting_doc = copy.deepcopy(settled_payload["api_run_accounting"])
            accounting_doc["previous_record"]["root_sha256"] = r1_dict.record_sha256
            settled_payload["api_run_accounting"] = accounting_doc
            settled_dict = SessionRecord._construct(**settled_payload)

            def local_opener(request, timeout=None):
                _ = timeout
                path = urlsplit(request.full_url).path
                bot = {"id": 999, "login": "reviewsensei[bot]", "type": "Bot"}
                if path == "/user" or path.startswith("/users/"):
                    return json_response(bot)
                if path.endswith("/issues/7/comments"):
                    rows = ([f.comment] if f.comment else []) + list(
                        f.extra_comments.values()
                    )
                    return json_response(copy.deepcopy(rows))
                cid = int(path.rsplit("/", 1)[1])
                return json_response(copy.deepcopy(f.extra_comments[cid]))

            budget = EvidenceReadBudget()
            http = GitHubHttp(api_url=f.api_url, opener=local_opener)
            ledger = GitHubIssueCommentSessionLedger(
                http, token="tok", app_slug="reviewsensei[bot]", evidence_budget=budget
            )
            self.assertEqual(ledger.load(IDENTITY).status, "ok")
            ledger._reuse_listed_parts = True
            earlier = _earlier_settled_effects(
                ledger,
                IDENTITY,
                settled_dict,
                ApiRunAccounting.from_document(settled_dict.api_run_accounting),
                same_head,
                999,
                budget,
                prior_record=r1_dict,
            )
            self.assertIsNot(earlier, _NO_EARLIER_EFFECTS)
            self.assertGreaterEqual(len(f.reviews), 1)
            self.assertGreaterEqual(len(f.inline), 1)
            self.assertGreaterEqual(len(f.check_runs), 1)
            self.assertTrue(
                all(earlier.explains(row, inline=False) for row in f.reviews)
            )
            self.assertTrue(
                all(
                    earlier.explains(
                        {**row, "original_commit_id": row["commit_id"]}, inline=True
                    )
                    for row in f.inline
                )
            )
            self.assertTrue(
                all(earlier.explains_check_run(row) for row in f.check_runs)
            )

    def test_bug4b_validate_before_checkpoint_handles_cached_partitioned_and_dictionary_lifecycles(
        self,
    ):
        from review_sensei_github import finding_lifecycle_dictionary as dict_codec
        from review_sensei_github.finding_lifecycle import _unpack_journal, _validate
        from review_sensei_github.review_run import ReviewRunHooks

        from review_sensei.bounded_evidence import (
            canonical_bytes,
            evidence_manifest,
            inline_manifest,
            partition_evidence,
            read_inline_evidence,
        )

        for variant in ("partitioned", "dictionary"):
            with (
                self.subTest(variant=variant),
                tempfile.TemporaryDirectory() as directory,
            ):
                f = self._blocking_and_advisory_round1(directory, limit=1024)
                r1 = parse_session_comment(f.comment["body"], identity=IDENTITY)
                orig_ref = r1.finding_lifecycle
                orig_manifest = orig_ref["manifest"]
                orig_binding = orig_manifest["binding"]
                orig_envelope = read_inline_evidence(
                    orig_manifest, expected_binding=orig_binding
                )

                base_parts = partition_evidence(
                    orig_envelope, binding=orig_binding, item_count=1
                )
                base_storage_ids: list[str] = []
                for idx, part in enumerate(base_parts):
                    cid = 2_000_100 + idx
                    f.extra_comments[cid] = {
                        "id": cid,
                        "issue_url": f"{f.api_url}/repos/owner/repo/issues/7",
                        "body": (
                            "ReviewSensei immutable evidence part v1\n```json\n"
                            + canonical_bytes(part).decode("utf-8")
                            + "\n```\n<!-- reviewsensei:evidence-part:v1 -->"
                        ),
                        "user": {
                            "type": "Bot",
                            "login": "reviewsensei[bot]",
                            "id": 999,
                        },
                    }
                    base_storage_ids.append(str(cid))
                base_part_manifest = evidence_manifest(
                    orig_envelope,
                    binding=orig_binding,
                    item_count=1,
                    parts=base_parts,
                    storage_ids=tuple(base_storage_ids),
                )

                r1_updated = _replace_finding_lifecycle(
                    r1, {**orig_ref, "manifest": base_part_manifest}
                )
                f.comment["body"] = render_session_comment(
                    repository_id=123, pull_request=7, record=r1_updated
                )

                f.next_review_run(head_sha=HEAD_2)
                reviews_before = len(f.reviews)
                checks_before = len(f.check_runs)

                orig_validate = ReviewRunHooks.validate_before_checkpoint
                if variant == "dictionary":
                    journal_doc = _validate(_unpack_journal(orig_envelope["lifecycle"]))
                    dict_payload = dict_codec.pack(
                        journal_doc,
                        base=orig_envelope,
                        manifest=base_part_manifest,
                        current_binding=orig_binding,
                    )
                    dict_envelope = {
                        "schema_version": "finding-lifecycle-evidence-v2",
                        "lifecycle": dict_payload,
                    }
                    dict_manifest = inline_manifest(
                        dict_envelope, binding=orig_binding, item_count=1
                    )

                    def wrapped_validate(hooks_self, **kwargs):
                        orig_effect_record = hooks_self.ledger.caller._effect_record
                        hooks_self.ledger.caller._effect_record = (
                            _replace_finding_lifecycle(
                                orig_effect_record,
                                {**orig_ref, "manifest": dict_manifest},
                            )
                        )
                        try:
                            return orig_validate(hooks_self, **kwargs)
                        finally:
                            hooks_self.ledger.caller._effect_record = orig_effect_record

                else:
                    wrapped_validate = orig_validate

                with (
                    patch.object(
                        ReviewRunHooks, "validate_before_checkpoint", wrapped_validate
                    ),
                    patch(
                        "review_sensei.finding_drift.project_drift_review_result",
                        side_effect=ReviewInputError(
                            "synthetic post-decode validation error"
                        ),
                    ) as mock_project,
                ):
                    self.assertEqual(f.invoke(), 1)
                self.assertEqual(mock_project.call_count, 1)

                self.assertIn("synthetic post-decode validation error", f.error)
                self.assertFalse(f.accounted._external_business_write_attempted)
                self.assertEqual(len(f.reviews), reviews_before)
                self.assertEqual(len(f.check_runs), checks_before)
                settled = parse_session_comment(f.comment["body"], identity=IDENTITY)
                self.assertIsNone(settled.transaction)
                self.assertEqual(settled.failed_attempts, 1)
                self.assertEqual(settled.failed_attempts_head_sha, HEAD_2)
                witnesses = _witnesses_from_extra(f.extra_comments)
                self.assertEqual(len(witnesses), 1)
                self.assertEqual(witnesses[0][2].boundary, "pre_checkpoint_validation")

    def test_bug2_failed_mutate_clears_admitted_prior_record_without_leak(self):
        from review_sensei_github import publication_failure_recovery as pfr

        with tempfile.TemporaryDirectory() as directory:
            f = self._blocking_and_advisory_round1(directory, limit=512)
            r1 = parse_session_comment(f.comment["body"], identity=IDENTITY)
            f.next_review_run(head_sha=HEAD_2)

            def fail_before_external_write(publisher_self, **kwargs):
                _ = publisher_self, kwargs
                raise GitHubPublicationError("synthetic round-2 pre-write crash")

            with patch.object(ReviewPublisher, "publish", fail_before_external_write):
                self.assertEqual(f.invoke(), 1)
            settled = parse_session_comment(f.comment["body"], identity=IDENTITY)

            ledger = f.selected.ledger
            orig_consume = pfr.consume_admitted_prior_record
            seen = []

            def failing_mutate(record):
                ledger._mutation_active = True
                pfr._ADMITTED_PRIOR_RECORDS[ledger] = (record.record_sha256, r1)
                raise RuntimeError("synthetic mutate failure after admission")

            with patch.object(
                pfr,
                "consume_admitted_prior_record",
                side_effect=lambda lgr, rec: seen.append(orig_consume(lgr, rec)),
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "synthetic mutate failure after admission"
                ):
                    ledger._native.replace(IDENTITY, failing_mutate) if hasattr(
                        ledger, "_native"
                    ) else ledger.replace(IDENTITY, failing_mutate)
            self.assertEqual(seen, [])
            self.assertIsNone(pfr.consume_admitted_prior_record(ledger, settled))

            # Follow-up rollover recovery succeeds cleanly with zero stale state.
            f.select_command("@reviewsensei review status", new_run=True)
            self.assertEqual(f.invoke_command(), 0, f.error + f.stdout)
            after = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertIsNone(after.transaction)
            self.assertEqual(after.completed_initial_reviews, 1)
            self.assertEqual(after.completed_verification_rounds, 0)

    def test_bug2_round1_pre_write_failure_resets_rescan_state_and_consecutive_failures(
        self,
    ):
        from review_sensei.api_work import ApiRunAccounting
        from review_sensei.bounded_evidence import evidence_manifest, partition_evidence

        with tempfile.TemporaryDirectory() as directory:
            f = PublicationFailureRecoveryFixture(directory, limit=512)
            f.provider_comments = [
                {
                    "path": "src/app.py",
                    "line": 2,
                    "side": "RIGHT",
                    "body": "Validate caller input before updating state.",
                    "severity": "high",
                    "blocking": True,
                    "needs_human": False,
                    "symbol": "apply_change",
                    "defect_kind": "missing_validation",
                }
            ]

            def fail_before_external_write(publisher_self, **kwargs):
                _ = publisher_self, kwargs
                raise GitHubPublicationError("synthetic round-1 pre-write crash")

            with patch.object(ReviewPublisher, "publish", fail_before_external_write):
                self.assertEqual(f.invoke(), 1)
            r1 = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertEqual(r1.transaction.phase, "publication_failed")
            self.assertEqual(r1.completed_initial_reviews, 1)
            self.assertIsNone(
                ApiRunAccounting.from_document(r1.api_run_accounting).previous_record
            )

            # Direct SessionRecord check: Round-1 pre-write rollover resets rescan_state too.
            from dataclasses import replace

            published_source = replace(
                r1.transaction, phase="publication_succeeded"
            ).to_dict()
            r1_with_rescan = r1._construct(
                **{
                    **{
                        k: v
                        for k, v in r1.to_dict().items()
                        if k not in {"schema_version", "record_sha256"}
                    },
                    "transaction": r1.transaction,
                    "rescan_state": {
                        "issue_comment": 10,
                        "workflow_dispatch": 0,
                        "receipts": [
                            {
                                "kind": "issue_comment",
                                "request_id": 10,
                                "review_id": 101,
                                "source": published_source,
                                "ordinary_reservation": "a" * 64,
                                "reservation": "b" * 64,
                            }
                        ],
                    },
                }
            )
            prev_binding = {
                "repository": IDENTITY.repository,
                "repository_id": IDENTITY.repository_id,
                "pull_request": IDENTITY.pull_request,
                "base_sha": r1.transaction.base_sha,
                "head_sha": r1.transaction.head_sha,
                "policy_digest": r1.transaction.policy_digest,
                "configuration_digest": r1.transaction.configuration_digest,
                "generation": r1_with_rescan.generation,
                "producer": "github-bot:999",
                "purpose": "evidence",
                "schema_version": "1.0",
            }
            prev_payload = r1_with_rescan.to_dict()
            prev_parts = partition_evidence(
                prev_payload, binding=prev_binding, item_count=1
            )
            prev_manifest = evidence_manifest(
                prev_payload,
                binding=prev_binding,
                item_count=1,
                parts=prev_parts,
                storage_ids=tuple(str(900 + i) for i in range(len(prev_parts))),
            )
            next_acc = {
                **r1.api_run_accounting,
                "identity": {
                    **r1.api_run_accounting["identity"],
                    "run_id": r1.api_run_accounting["identity"]["run_id"] + 1,
                },
                "owner": {"run_attempt": 1, "job": "hosted", "generation": 0},
                "completed": [],
                "prior_nonstartup_liability": 0,
                "blocks": [],
                "previous_record": {
                    "run_id": r1.api_run_accounting["identity"]["run_id"],
                    "root_sha256": r1_with_rescan.record_sha256,
                    "manifest": prev_manifest,
                },
                "action": None,
                "analysis_abort": None,
                "terminal_transaction": None,
            }
            rolled = r1_with_rescan.rollover_pre_write_settled_api_accounting(next_acc)
            self.assertEqual(rolled.completed_initial_reviews, 0)
            self.assertEqual(rolled.completed_verification_rounds, 0)
            self.assertIsNone(rolled.convergence_history)
            self.assertIsNone(rolled.rescan_state)

            # Consecutive Round-1 pre-write failures: Run 2 rolls over R1 (resets to 0, 0)
            # and crashes pre-write again; Run 3 walks past R1 (publication_failed) -> None.
            f.next_review_run(head_sha=HEAD_2)
            with patch.object(ReviewPublisher, "publish", fail_before_external_write):
                self.assertEqual(f.invoke(), 1)
            r2 = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertEqual(r2.transaction.phase, "publication_failed")
            self.assertEqual(r2.completed_initial_reviews, 1)

            f.select_command("@reviewsensei review status", new_run=True)
            self.assertEqual(f.invoke_command(), 0, f.error + f.stdout)
            restored = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertIsNone(restored.transaction)
            self.assertEqual(restored.completed_initial_reviews, 0)
            self.assertEqual(restored.completed_verification_rounds, 0)
            self.assertIsNone(restored.convergence_history)
            self.assertIsNone(restored.rescan_state)

            f.next_review_run(head_sha=HEAD_3)
            self.assertEqual(f.invoke(), 0, f.error)
            r4 = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertEqual(r4.transaction.phase, "publication_succeeded")
            self.assertEqual(r4.completed_initial_reviews, 1)
            self.assertEqual(r4.completed_verification_rounds, 0)

    def test_bug2_consecutive_round2_pre_write_failures_walk_to_settled_ancestor(self):
        with tempfile.TemporaryDirectory() as directory:
            f = PublicationFailureRecoveryFixture(directory, limit=512)
            f.provider_comments = [
                {
                    "path": "src/app.py",
                    "line": 2,
                    "side": "RIGHT",
                    "body": "Validate caller input before updating state.",
                    "severity": "high",
                    "blocking": True,
                    "needs_human": False,
                    "symbol": "apply_change",
                    "defect_kind": "missing_validation",
                }
            ]
            self.assertEqual(f.invoke(), 0, f.error)
            r1 = parse_session_comment(f.comment["body"], identity=IDENTITY)
            r1_history = copy.deepcopy(r1.convergence_history)

            def fail_before_external_write(publisher_self, **kwargs):
                _ = publisher_self, kwargs
                raise GitHubPublicationError("synthetic round-2+ pre-write crash")

            # Run 2 fails pre-write at Round 2 on HEAD_2.
            f.next_review_run(head_sha=HEAD_2)
            with patch.object(ReviewPublisher, "publish", fail_before_external_write):
                self.assertEqual(f.invoke(), 1)
            r2 = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertEqual(r2.transaction.phase, "publication_failed")
            self.assertEqual(r2.completed_verification_rounds, 1)

            # Run 3 directly rolls over R2 (restores R1 baseline), advances to Round 2
            # on HEAD_3, and fails pre-write again!
            f.next_review_run(head_sha=HEAD_3)
            with patch.object(ReviewPublisher, "publish", fail_before_external_write):
                self.assertEqual(f.invoke(), 1)
            r3 = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertEqual(r3.transaction.phase, "publication_failed")
            self.assertEqual(r3.completed_verification_rounds, 1)
            self.assertEqual(
                r3.convergence_history["baseline"]["cache_key"]["head_sha"], HEAD_3
            )

            # Recovery walks past R2 (publication_failed) back to R1 (publication_succeeded)
            # instead of resurrecting R2's unpublished HEAD_2 / verification_rounds=1 state.
            f.select_command("@reviewsensei review status", new_run=True)
            self.assertEqual(f.invoke_command(), 0, f.error + f.stdout)
            restored = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertIsNone(restored.transaction)
            self.assertEqual(restored.completed_initial_reviews, 1)
            self.assertEqual(restored.completed_verification_rounds, 0)
            self.assertEqual(
                restored.convergence_history["baseline"], r1_history["baseline"]
            )
            self.assertEqual(
                restored.convergence_history["progress"], r1_history["progress"]
            )

            # Subsequent review on HEAD_4 succeeds with verification_rounds == 1.
            head_4 = "f" * 40
            f.next_review_run(head_sha=head_4)
            self.assertEqual(f.invoke(), 0, f.error)
            r5 = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertEqual(r5.transaction.phase, "publication_succeeded")
            self.assertEqual(r5.completed_initial_reviews, 1)
            self.assertEqual(r5.completed_verification_rounds, 1)
            self.assertEqual(
                r5.convergence_history["baseline"]["cache_key"]["head_sha"], head_4
            )

    def test_bug2_pre_validation_and_hop_bound_refuse_before_archive_post(self):
        import hashlib

        from review_sensei_github import publication_failure_recovery as pfr

        from review_sensei.bounded_evidence import canonical_bytes

        with tempfile.TemporaryDirectory() as directory:
            f = self._blocking_and_advisory_round1(directory, limit=512)
            r1 = parse_session_comment(f.comment["body"], identity=IDENTITY)
            f.next_review_run(head_sha=HEAD_2)

            def fail_before_external_write(publisher_self, **kwargs):
                _ = publisher_self, kwargs
                raise GitHubPublicationError("synthetic pre-write crash")

            with patch.object(ReviewPublisher, "publish", fail_before_external_write):
                self.assertEqual(f.invoke(), 1)
            settled = parse_session_comment(f.comment["body"], identity=IDENTITY)

            # Corrupt prior_record returned by read_previous_run to have higher counters than settled.
            inflated_r1 = SessionRecord._construct(
                **{
                    **{
                        k: v
                        for k, v in r1.to_dict().items()
                        if k not in {"schema_version", "record_sha256"}
                    },
                    "transaction": r1.transaction,
                    "completed_verification_rounds": settled.completed_verification_rounds
                    + 1,
                }
            )
            bad_settled = SessionRecord._construct(
                **{
                    **{
                        k: v
                        for k, v in settled.to_dict().items()
                        if k not in {"schema_version", "record_sha256"}
                    },
                    "transaction": settled.transaction,
                    "api_run_accounting": {
                        **settled.api_run_accounting,
                        "previous_record": {
                            **settled.api_run_accounting["previous_record"],
                            "root_sha256": inflated_r1.record_sha256,
                        },
                    },
                }
            )
            f.comment["body"] = render_session_comment(
                repository_id=123, pull_request=7, record=bad_settled
            )
            cid, _, w = _witnesses_from_extra(f.extra_comments)[0]
            saved_witness_body = f.extra_comments[cid]["body"]

            def _signed_witness_body(target_record: SessionRecord) -> str:
                payload = {
                    **w.to_dict(),
                    "settled_root_sha256": target_record.record_sha256,
                    "settled_generation": target_record.generation,
                }
                unsigned = {k: v for k, v in payload.items() if k != "content_sha256"}
                payload["content_sha256"] = hashlib.sha256(
                    canonical_bytes(unsigned)
                ).hexdigest()
                encoded = json.dumps(
                    payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
                )
                return (
                    "ReviewSensei recorded a pre-external-write failure settlement witness "
                    "(data only; requires independent Actions cessation and native negative census).\n\n"
                    f"```json\n{encoded}\n```\n"
                    f"{SETTLEMENT_WITNESS_MARKER}"
                )

            f.extra_comments[cid]["body"] = _signed_witness_body(bad_settled)
            extra_before = len(f.extra_comments)
            f.select_command("@reviewsensei review status", new_run=True)
            with patch.object(pfr, "read_previous_run", return_value=inflated_r1):
                self.assertNotEqual(f.invoke_command(), 0)
            self.assertIn(
                "prior settled session root conflicts with predecessor", f.error
            )
            self.assertEqual(len(f.extra_comments), extra_before)

            # Walk > 8 hops of publication_failed roots fails closed before archive POST.
            base_ref = settled.api_run_accounting["previous_record"]
            settled_fields = {
                k: v
                for k, v in settled.to_dict().items()
                if k not in {"schema_version", "record_sha256"}
            }
            chain_by_key: dict[tuple[int, str], SessionRecord] = {}
            prev_ref = copy.deepcopy(base_ref)
            base_gen = r1.generation
            for step in range(1, 10):
                hop_run_id = 700 + step
                hop_rec = SessionRecord._construct(
                    **{
                        **settled_fields,
                        "generation": base_gen + step,
                        "transaction": settled.transaction,
                        "api_run_accounting": {
                            **settled.api_run_accounting,
                            "identity": {
                                **settled.api_run_accounting["identity"],
                                "run_id": hop_run_id,
                            },
                            "previous_record": prev_ref,
                        },
                    }
                )
                chain_by_key[(hop_run_id, hop_rec.record_sha256)] = hop_rec
                prev_ref = {
                    **base_ref,
                    "run_id": hop_run_id,
                    "root_sha256": hop_rec.record_sha256,
                }

            loop_settled = SessionRecord._construct(
                **{
                    **settled_fields,
                    "generation": base_gen + 10,
                    "transaction": settled.transaction,
                    "api_run_accounting": {
                        **settled.api_run_accounting,
                        "previous_record": prev_ref,
                    },
                }
            )
            f.comment["body"] = render_session_comment(
                repository_id=123, pull_request=7, record=loop_settled
            )
            f.extra_comments[cid]["body"] = _signed_witness_body(loop_settled)
            extra_before_loop = len(f.extra_comments)
            f.select_command("@reviewsensei review status", new_run=True)
            with patch.object(
                pfr,
                "read_previous_run",
                side_effect=lambda _ledger, _identity, ref: chain_by_key[
                    (ref["run_id"], ref["root_sha256"])
                ],
            ):
                self.assertNotEqual(f.invoke_command(), 0)
            self.assertIn("archive walk bound", f.error)
            self.assertEqual(len(f.extra_comments), extra_before_loop)
            f.extra_comments[cid]["body"] = saved_witness_body

    def test_bug2_earlier_settled_effects_accepts_intervening_terminal_command_on_same_head(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            f = self._blocking_and_advisory_round1(directory, limit=512)
            reviews_before = len(f.reviews)
            checks_before = len(f.check_runs)

            # Intervening terminal command on same_head leaves action.phase == "terminal"
            # and terminal_transaction is None while retaining Round 1's transaction.
            f.select_command("@reviewsensei review status", new_run=True)
            self.assertEqual(f.invoke_command(), 0, f.error + f.stdout)
            after_cmd = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertEqual(
                after_cmd.api_run_accounting["action"]["phase"], "terminal"
            )
            self.assertIsNone(after_cmd.api_run_accounting["terminal_transaction"])

            # Pre-write publication failure on HEAD_2 after the terminal command on same_head
            # (after_cmd still holds action.phase == "terminal" and terminal_transaction is None
            # while retaining Round 1's transaction).
            f.next_review_run(head_sha=HEAD_2)

            def fail_before_external_write(publisher_self, **kwargs):
                _ = publisher_self, kwargs
                raise GitHubPublicationError(
                    "synthetic pre-write crash after terminal command"
                )

            with patch.object(ReviewPublisher, "publish", fail_before_external_write):
                self.assertEqual(f.invoke(), 1)
            self.assertEqual(len(f.reviews), reviews_before)
            self.assertEqual(len(f.check_runs), checks_before)
            self.assertEqual(len(_witnesses_from_extra(f.extra_comments)), 1)

            # Recovery must accept R1's existing reviews/inline comments/check-runs
            # via the intervening terminal command ancestor.
            f.select_command("@reviewsensei review status", new_run=True)
            self.assertEqual(f.invoke_command(), 0, f.error + f.stdout)
            restored = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertIsNone(restored.transaction)
            self.assertEqual(restored.completed_initial_reviews, 1)
            self.assertEqual(restored.completed_verification_rounds, 0)

    def test_bug4_read_cached_manifest_in_memory_validates_binding_and_producer(self):
        from review_sensei_github.http import GitHubHttp
        from review_sensei_github.review_run import _read_cached_manifest_in_memory
        from review_sensei_github.session_ledger import GitHubIssueCommentSessionLedger

        from review_sensei.bounded_evidence import inline_manifest

        base_binding = {
            "repository": IDENTITY.repository,
            "repository_id": IDENTITY.repository_id,
            "pull_request": IDENTITY.pull_request,
            "base_sha": "a" * 40,
            "head_sha": "b" * 40,
            "policy_digest": "1" * 64,
            "configuration_digest": "2" * 64,
            "generation": 2,
            "producer": "github-bot:999",
            "purpose": "finding-lifecycle",
            "schema_version": "1.0",
        }
        payload = {"schema_version": "finding-lifecycle-evidence-v2", "lifecycle": {}}
        http = GitHubHttp(
            api_url="https://api.github.test",
            opener=lambda _r, timeout=None: json_response(
                {"id": 999, "login": "reviewsensei[bot]", "type": "Bot"}
            ),
        )
        ledger = GitHubIssueCommentSessionLedger(
            http, token="tok", app_slug="reviewsensei[bot]"
        )
        ledger._evidence_author_id = 999

        valid_manifest = inline_manifest(payload, binding=base_binding, item_count=1)
        self.assertEqual(
            _read_cached_manifest_in_memory(
                ledger, IDENTITY, valid_manifest, max_generation=3
            ),
            payload,
        )

        for field, bad_value in (
            ("repository", "other/repo"),
            ("repository_id", 888),
            ("pull_request", 99),
            ("producer", "github-bot:123"),
            ("generation", 5),
        ):
            with self.subTest(field=field):
                bad_binding = {**base_binding, field: bad_value}
                bad_manifest = inline_manifest(
                    payload, binding=bad_binding, item_count=1
                )
                with self.assertRaisesRegex(
                    ReviewInputError, "finding lifecycle manifest binding conflicts"
                ):
                    _read_cached_manifest_in_memory(
                        ledger, IDENTITY, bad_manifest, max_generation=3
                    )

        for bad_generation in (-1, True):
            with self.subTest(bad_generation=bad_generation):
                bad_binding = {**base_binding, "generation": bad_generation}
                bad_manifest = {
                    **valid_manifest,
                    "binding": bad_binding,
                }
                with self.assertRaisesRegex(
                    ReviewInputError, "partition integer exceeds its contract"
                ):
                    _read_cached_manifest_in_memory(
                        ledger, IDENTITY, bad_manifest, max_generation=3
                    )

        class _NoProducerLedger:
            pass

        with self.assertRaisesRegex(
            ReviewInputError, "finding lifecycle manifest producer is unavailable"
        ):
            _read_cached_manifest_in_memory(
                _NoProducerLedger(), IDENTITY, valid_manifest, max_generation=3
            )

    def test_admitted_prior_chain_peek_consume_binding_clear_and_hop_bound(self):
        from review_sensei_github import publication_failure_recovery as pfr
        from review_sensei_github.http import GitHubHttp
        from review_sensei_github.session_ledger import GitHubIssueCommentSessionLedger

        http = GitHubHttp(
            api_url="https://api.github.test",
            opener=lambda _r, timeout=None: json_response(
                {"id": 999, "login": "reviewsensei[bot]", "type": "Bot"}
            ),
        )
        ledger = GitHubIssueCommentSessionLedger(
            http, token="tok", app_slug="reviewsensei[bot]"
        )
        with tempfile.TemporaryDirectory() as directory:
            f = PublicationFailureRecoveryFixture(directory, limit=512)
            self.assertEqual(f.invoke(), 0, f.error)
            r1 = parse_session_comment(f.comment["body"], identity=IDENTITY)
            f.next_review_run(head_sha=HEAD_2)

            def fail_before_write(publisher_self, **kwargs):
                _ = publisher_self, kwargs
                raise GitHubPublicationError("synthetic crash")

            with patch.object(ReviewPublisher, "publish", fail_before_write):
                self.assertEqual(f.invoke(), 1)
            r2 = parse_session_comment(f.comment["body"], identity=IDENTITY)

            # 1. Peek is idempotent; wrong record_sha256 returns () without clearing.
            pfr._ADMITTED_PRIOR_CHAINS[ledger] = (r2.record_sha256, (r1,))
            self.assertEqual(pfr.peek_admitted_prior_chain(ledger, r1), ())
            self.assertEqual(pfr.peek_admitted_prior_chain(ledger, r2), (r1,))
            self.assertEqual(pfr.peek_admitted_prior_chain(ledger, r2), (r1,))

            # 2. Mismatched consume returns () and pops; matched consume returns once then ().
            self.assertEqual(pfr.consume_admitted_prior_chain(ledger, r1), ())
            self.assertEqual(pfr.peek_admitted_prior_chain(ledger, r2), ())
            pfr._ADMITTED_PRIOR_CHAINS[ledger] = (r2.record_sha256, (r1,))
            self.assertEqual(pfr.consume_admitted_prior_chain(ledger, r2), (r1,))
            self.assertEqual(pfr.consume_admitted_prior_chain(ledger, r2), ())

            # 3. clear_admitted_prior_record drops both prior record and ancestor chain.
            pfr._ADMITTED_PRIOR_RECORDS[ledger] = (r2.record_sha256, r1)
            pfr._ADMITTED_PRIOR_CHAINS[ledger] = (r2.record_sha256, (r1,))
            pfr.clear_admitted_prior_record(ledger)
            self.assertIsNone(pfr.consume_admitted_prior_record(ledger, r2))
            self.assertEqual(pfr.peek_admitted_prior_chain(ledger, r2), ())

            # 4. Hop bound > 8 in SessionRecord.rollover_pre_write_settled_api_accounting refuses.
            f.next_review_run(head_sha=HEAD_3)
            self.assertEqual(f.invoke(), 0, f.error)
            after_rollover = parse_session_comment(f.comment["body"], identity=IDENTITY)
            candidate_doc = {
                **copy.deepcopy(after_rollover.api_run_accounting),
                "completed": [],
                "blocks": [],
                "action": None,
                "analysis_abort": None,
                "terminal_transaction": None,
                "prior_nonstartup_liability": 0,
                "startup_used": [["hosted", 1]],
            }
            with self.assertRaisesRegex(
                ReviewInputError, "pre-write-settled rollover ancestor chain is invalid"
            ):
                r2.rollover_pre_write_settled_api_accounting(
                    candidate_doc,
                    prior_record=None,
                    skipped_failed_ancestors=(r2,) * 9,
                )

    def test_earlier_settled_effects_protects_multipart_expected_archive_and_monotonic_ancestors(
        self,
    ):
        import hashlib

        from review_sensei_github import publication_failure_recovery as pfr
        from review_sensei_github.api_run_history import RETAINED_CONFIRMATION_TAIL

        from review_sensei.api_work import ApiRunAccounting
        from review_sensei.bounded_evidence import (
            MAX_PART_READS,
            EvidenceReadBudget,
            canonical_bytes,
        )

        with tempfile.TemporaryDirectory() as directory:
            f = self._blocking_and_advisory_round1(directory, limit=512)
            r1 = parse_session_comment(f.comment["body"], identity=IDENTITY)

            def fail_before_external_write(publisher_self, **kwargs):
                _ = publisher_self, kwargs
                raise GitHubPublicationError("synthetic pre-write crash")

            f.next_review_run(head_sha=HEAD_2)
            with patch.object(ReviewPublisher, "publish", fail_before_external_write):
                self.assertEqual(f.invoke(), 1)
            r2 = parse_session_comment(f.comment["body"], identity=IDENTITY)

            # Part A: _earlier_settled_effects protects multi-part prior_archive_ref reads.
            three_part_ref = copy.deepcopy(r2.api_run_accounting["previous_record"])
            three_part_ref["manifest"]["parts"] = [
                {"storage_id": 101},
                {"storage_id": 102},
                {"storage_id": 103},
            ]
            current_rec = _replace_finding_lifecycle(r2, r1.finding_lifecycle)
            manifest_parts = len(
                current_rec.finding_lifecycle["manifest"].get("parts", ())
            )
            underfunded_budget = EvidenceReadBudget()
            underfunded_budget.calls = (
                MAX_PART_READS - 4 - RETAINED_CONFIRMATION_TAIL - (manifest_parts + 2)
            )
            ledger_stub = object()
            with patch.object(
                pfr,
                "read_previous_run",
                side_effect=AssertionError("must fail _protect_tail before reading"),
            ):
                with self.assertRaisesRegex(
                    ReviewInputError, "native effect census is unfunded"
                ):
                    pfr._earlier_settled_effects(
                        ledger_stub,  # type: ignore[arg-type]
                        IDENTITY,
                        current_rec,
                        ApiRunAccounting.from_document(r2.api_run_accounting),
                        r1.finding_lifecycle["manifest"]["binding"]["head_sha"],
                        999,
                        underfunded_budget,
                        prior_record=None,
                        prior_archive_ref=three_part_ref,
                    )

            # Part B: monotonic check across skipped-failed ancestors against prior_record.
            f.next_review_run(head_sha=HEAD_3)
            with patch.object(ReviewPublisher, "publish", fail_before_external_write):
                self.assertEqual(f.invoke(), 1)
            r3 = parse_session_comment(f.comment["body"], identity=IDENTITY)
            r1_fields = {
                k: v
                for k, v in r1.to_dict().items()
                if k not in {"schema_version", "record_sha256"}
            }
            inflated_r1 = SessionRecord._construct(
                **{
                    **r1_fields,
                    "transaction": r1.transaction,
                    "completed_verification_rounds": r1.completed_verification_rounds
                    + 1,
                }
            )
            r2_zero_rounds_fields = {
                k: v
                for k, v in r2.to_dict().items()
                if k not in {"schema_version", "record_sha256"}
            }
            r2_zero_rounds = SessionRecord._construct(
                **{
                    **r2_zero_rounds_fields,
                    "transaction": r2.transaction,
                    "completed_verification_rounds": 0,
                    "api_run_accounting": {
                        **r2.api_run_accounting,
                        "previous_record": {
                            **r2.api_run_accounting["previous_record"],
                            "root_sha256": inflated_r1.record_sha256,
                        },
                    },
                }
            )
            r3_one_round_fields = {
                k: v
                for k, v in r3.to_dict().items()
                if k not in {"schema_version", "record_sha256"}
            }
            r3_one_round = SessionRecord._construct(
                **{
                    **r3_one_round_fields,
                    "transaction": r3.transaction,
                    "completed_verification_rounds": 1,
                    "api_run_accounting": {
                        **r3.api_run_accounting,
                        "previous_record": {
                            **r3.api_run_accounting["previous_record"],
                            "root_sha256": r2_zero_rounds.record_sha256,
                        },
                    },
                }
            )
            r3_ref = r3_one_round.api_run_accounting["previous_record"]
            r2_ref = r2_zero_rounds.api_run_accounting["previous_record"]
            by_key = {
                (r3_ref["run_id"], r3_ref["root_sha256"]): r2_zero_rounds,
                (r2_ref["run_id"], r2_ref["root_sha256"]): inflated_r1,
            }
            f.comment["body"] = render_session_comment(
                repository_id=123, pull_request=7, record=r3_one_round
            )
            cid, _, w = _witnesses_from_extra(f.extra_comments)[-1]
            payload = {
                **w.to_dict(),
                "settled_root_sha256": r3_one_round.record_sha256,
                "settled_generation": r3_one_round.generation,
            }
            unsigned = {k: v for k, v in payload.items() if k != "content_sha256"}
            payload["content_sha256"] = hashlib.sha256(
                canonical_bytes(unsigned)
            ).hexdigest()
            encoded = json.dumps(
                payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            )
            f.extra_comments[cid]["body"] = (
                "ReviewSensei recorded a pre-external-write failure settlement witness "
                "(data only; requires independent Actions cessation and native negative census).\n\n"
                f"```json\n{encoded}\n```\n"
                f"{SETTLEMENT_WITNESS_MARKER}"
            )
            f.select_command("@reviewsensei review status", new_run=True)
            with patch.object(
                pfr,
                "read_previous_run",
                side_effect=lambda _ledger, _identity, ref: by_key[
                    (ref["run_id"], ref["root_sha256"])
                ],
            ):
                self.assertNotEqual(f.invoke_command(), 0)
            self.assertIn(
                "prior settled session root conflicts with predecessor", f.error
            )

            # SessionRecord.rollover_pre_write_settled_api_accounting enforces the same check.
            candidate_doc = {
                **copy.deepcopy(r3.api_run_accounting),
                "identity": {"repository_id": 123, "run_id": 9991},
                "owner": {"run_attempt": 1, "job": "hosted", "generation": 0},
                "completed": [],
                "blocks": [],
                "action": None,
                "analysis_abort": None,
                "terminal_transaction": None,
                "prior_nonstartup_liability": 0,
                "startup_used": [["hosted", 1]],
                "previous_record": {
                    **copy.deepcopy(r3_ref),
                    "run_id": r3.api_run_accounting["identity"]["run_id"],
                    "root_sha256": r3_one_round.record_sha256,
                    "manifest": {
                        **copy.deepcopy(r3_ref["manifest"]),
                        "binding": {
                            **copy.deepcopy(r3_ref["manifest"]["binding"]),
                            "generation": r3_one_round.generation,
                        },
                    },
                },
            }
            with self.assertRaisesRegex(
                ReviewInputError,
                "pre-write-settled rollover prior_record conflicts with previous_record",
            ):
                r3_one_round.rollover_pre_write_settled_api_accounting(
                    candidate_doc,
                    prior_record=inflated_r1,
                    skipped_failed_ancestors=(r2_zero_rounds,),
                )

            # Positive case: earlier failed ancestor speculatively incremented (+1) while
            # later failed child remained at prior_record's settled counter (0) -> accepted.
            r2_one_round = SessionRecord._construct(
                **{
                    **r2_zero_rounds_fields,
                    "transaction": r2.transaction,
                    "completed_verification_rounds": 1,
                }
            )
            r3_zero_round = SessionRecord._construct(
                **{
                    **r3_one_round_fields,
                    "transaction": r3.transaction,
                    "completed_verification_rounds": 0,
                    "api_run_accounting": {
                        **r3.api_run_accounting,
                        "previous_record": {
                            **r3_ref,
                            "root_sha256": r2_one_round.record_sha256,
                        },
                    },
                }
            )
            r3_zero_doc = {
                **candidate_doc,
                "previous_record": {
                    **candidate_doc["previous_record"],
                    "root_sha256": r3_zero_round.record_sha256,
                },
            }
            accepted_rollover = r3_zero_round.rollover_pre_write_settled_api_accounting(
                r3_zero_doc,
                prior_record=r1,
                skipped_failed_ancestors=(r2_one_round,),
            )
            self.assertEqual(accepted_rollover.completed_initial_reviews, 1)
            self.assertEqual(accepted_rollover.completed_verification_rounds, 0)

    def test_path_a_rejects_foreign_or_malformed_rejected_check_run_patch(self):
        from review_sensei_github.publication_failure_recovery import (
            _inspect_confirmed_failed_tx_lifecycle,
        )

        from review_sensei.api_work import ApiRunAccounting
        from review_sensei.bounded_evidence import EvidenceReadBudget

        with tempfile.TemporaryDirectory() as directory:
            f = self._blocking_and_advisory_round1(directory, limit=1024)
            f.next_review_run(head_sha=HEAD_2)
            checks_before = len(f.check_runs)

            def fail_after_check_run(pub_self, **_kwargs):
                _ = pub_self, _kwargs
                raise GitHubPublicationError("drift preflight failed after check-run")

            with patch.object(
                ReviewPublisher, "_update_existing_drift_threads", fail_after_check_run
            ):
                self.assertEqual(f.invoke(), 1)
            self.assertGreater(len(f.check_runs), checks_before)
            settled = parse_session_comment(f.comment["body"], identity=IDENTITY)
            created_check_id = f.check_runs[-1]["id"]

            for bad_patch_path in (
                f"/repos/owner/repo/check-runs/{created_check_id + 999}",
                "/repos/owner/repo/check-runs/not-an-id",
            ):
                fake_doc = {
                    "schema_version": "finding-lifecycle-journal-v2",
                    "unknown_trust_effect": False,
                    "native_receipts": [],
                    "effects": [
                        {
                            "method": "POST",
                            "path": "/repos/owner/repo/check-runs",
                            "status": "confirmed",
                            "request": {
                                "head_sha": HEAD_2,
                                "name": "ReviewSensei Review Gate",
                            },
                            "response": [201, {"id": created_check_id}],
                        },
                        {
                            "method": "PATCH",
                            "path": bad_patch_path,
                            "status": "rejected",
                            "request": {"status": "completed"},
                            "response": [404, {"message": "Not Found"}],
                        },
                    ],
                }
                with (
                    patch(
                        "review_sensei.finding_lifecycle_store._decode_selected_lifecycle",
                        return_value=fake_doc,
                    ),
                    self.assertRaisesRegex(
                        ReviewInputError,
                        "finding lifecycle already recorded effects for the failed transaction",
                    ),
                ):
                    _inspect_confirmed_failed_tx_lifecycle(
                        f.ledger,
                        IDENTITY,
                        settled,
                        ApiRunAccounting.from_document(settled.api_run_accounting),
                        EvidenceReadBudget(),
                        app_slug="reviewsensei[bot]",
                    )

    def test_unwitnessed_no_transaction_startup_crash_recovery_and_fail_closed_gates(
        self,
    ):
        from dataclasses import replace

        from review_sensei.api_work import ApiRunAccounting, PendingApiRequest

        def _make_startup_crash_fixture(
            directory: str,
            *,
            crashed_head: str,
            live_head: str,
            run_conclusion: str = "failure",
            run_status: str = "completed",
            job_conclusion: str = "failure",
            confirmed: int = 0,
        ):
            f = PublicationFailureRecoveryFixture(directory, limit=1024)
            f.live_head = f.requested_head = crashed_head

            def crash_after_activation() -> None:
                raise RuntimeError("synthetic startup crash after activation")

            with (
                patch.object(f, "before_entry", crash_after_activation),
                self.assertRaises(RuntimeError),
            ):
                f.invoke()
            crashed_run_id = f.run_id
            rec = parse_session_comment(f.comment["body"], identity=IDENTITY)
            assert rec is not None
            self.assertIsNone(rec.transaction)
            acc = ApiRunAccounting.from_document(rec.api_run_accounting)
            clean_blocks = tuple(replace(b, pending=()) for b in acc.blocks)
            completed = (
                (PendingApiRequest("confirmed_1", 9_999_999_999_999),)
                if confirmed > 0
                else ()
            )
            clean_acc = replace(
                acc,
                blocks=clean_blocks,
                completed=completed,
                analysis_abort=None,
                terminal_transaction=None,
            )
            payload = rec.to_dict()
            del payload["schema_version"], payload["record_sha256"]
            payload["generation"] = rec.generation + 1
            payload["transaction"] = None
            payload["reservation_id"] = None
            payload["reserved_slot"] = None
            payload["publication_staging"] = None
            payload["failed_attempts"] = 0
            payload["failed_attempts_head_sha"] = None
            payload["api_run_accounting"] = clean_acc.to_document()
            crashed_rec = SessionRecord._construct(**payload)
            f.comment["body"] = render_session_comment(
                repository_id=123, pull_request=7, record=crashed_rec
            )
            f.previous_conclusion = run_conclusion
            f.next_review_run(head_sha=live_head)
            base_external = f.external

            def wrapped_external(request, timeout):
                response = base_external(request, timeout)
                path = urlsplit(request.full_url).path
                if path == f"/repos/owner/repo/actions/runs/{crashed_run_id}":
                    value = json.loads(response.read(524288))
                    value["status"] = run_status
                    value["conclusion"] = run_conclusion
                    value["head_sha"] = crashed_head
                    return json_response(value)
                if path.startswith(
                    f"/repos/owner/repo/actions/runs/{crashed_run_id}/attempts/"
                ) and path.endswith("/jobs"):
                    value = json.loads(response.read(524288))
                    value["jobs"][0]["conclusion"] = job_conclusion
                    return json_response(value)
                return response

            f.external = wrapped_external
            return f, crashed_rec

        # 1) Confirmed == 0, completed non-success startup crash recovers automatically
        #    both on the same head and when the live PR moved to a newer head.
        for conclusion in ("failure", "cancelled", "timed_out", "startup_failure"):
            for live_head in ("a" * 40, HEAD_2):
                with (
                    self.subTest(conclusion=conclusion, moved=live_head != "a" * 40),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    f, _ = _make_startup_crash_fixture(
                        directory,
                        crashed_head="a" * 40,
                        live_head=live_head,
                        run_conclusion=conclusion,
                        job_conclusion=conclusion,
                    )
                    self.assertEqual(f.invoke(), 0, f.error)
                    final = parse_session_comment(f.comment["body"], identity=IDENTITY)
                    self.assertIsNotNone(final.transaction)
                    self.assertEqual(final.transaction.phase, "publication_succeeded")

        # 2) Fail closed on active run, success conclusion, success job, or confirmed > 0.
        for label, overrides in (
            ("in_progress", {"run_status": "in_progress", "run_conclusion": "failure"}),
            ("run_success", {"run_conclusion": "success"}),
            ("job_success", {"job_conclusion": "success"}),
            ("confirmed_nonzero", {"confirmed": 1}),
        ):
            with self.subTest(case=label), tempfile.TemporaryDirectory() as directory:
                f, _ = _make_startup_crash_fixture(
                    directory,
                    crashed_head="a" * 40,
                    live_head=HEAD_2,
                    **overrides,
                )
                self.assertEqual(f.invoke(), 1)
                if label == "confirmed_nonzero":
                    self.assertIn("phase=no-transaction", f.error)

        # 3) Unparseable unrelated bot carrier/manifest comments must NOT falsely block recovery,
        #    while a carrier referencing the crashed root/run/generation or a native effect blocks it.
        for label, body in (
            (
                "malformed_evidence_part",
                "ReviewSensei immutable evidence part v1\n```json\n{not-json\n```\n<!-- reviewsensei:evidence-part:v1 -->",
            ),
            (
                "malformed_native_manifest",
                "ReviewSensei immutable native publication manifest v1\n```json\n{not-json\n```\n<!-- reviewsensei:native-manifest:v1 -->",
            ),
        ):
            with (
                self.subTest(carrier=label),
                tempfile.TemporaryDirectory() as directory,
            ):
                f, _ = _make_startup_crash_fixture(
                    directory, crashed_head="a" * 40, live_head=HEAD_2
                )
                f.extra_comments[900000] = {
                    "id": 900000,
                    "body": body,
                    "user": {"id": 999, "login": "reviewsensei[bot]", "type": "Bot"},
                    "issue_url": f"{f.api_url}/repos/owner/repo/issues/7",
                }
                self.assertEqual(f.invoke(), 0, f.error)

        with tempfile.TemporaryDirectory() as directory:
            f, crashed_rec = _make_startup_crash_fixture(
                directory, crashed_head="a" * 40, live_head=HEAD_2
            )
            f.extra_comments[900001] = {
                "id": 900001,
                "body": (
                    "ReviewSensei immutable evidence part v1\n```json\n"
                    + json.dumps({"unexpected": crashed_rec.record_sha256})
                    + "\n```\n<!-- reviewsensei:evidence-part:v1 -->"
                ),
                "user": {"id": 999, "login": "reviewsensei[bot]", "type": "Bot"},
                "issue_url": f"{f.api_url}/repos/owner/repo/issues/7",
            }
            self.assertEqual(f.invoke(), 1)
            self.assertIn("phase=no-transaction", f.error)

        for check_head in ("a" * 40, HEAD_2):
            with (
                self.subTest(check_head=check_head),
                tempfile.TemporaryDirectory() as directory,
            ):
                f, _ = _make_startup_crash_fixture(
                    directory, crashed_head="a" * 40, live_head=HEAD_2
                )
                f.check_runs.append(
                    {
                        "id": 88001,
                        "name": "ReviewSensei",
                        "head_sha": check_head,
                        "app": {"slug": "reviewsensei"},
                        "status": "completed",
                        "conclusion": "neutral",
                    }
                )
                self.assertEqual(f.invoke(), 1)
                self.assertIn("phase=no-transaction", f.error)

    def test_is_startup_crash_predecessor_rejects_non_initial_or_prior_liability_blocks(
        self,
    ) -> None:
        from dataclasses import replace

        from review_sensei.api_work import ApiRunAccounting, CallerRequestBlock
        from review_sensei.session import is_startup_crash_predecessor

        with tempfile.TemporaryDirectory() as directory:
            f = PublicationFailureRecoveryFixture(directory, limit=1024)
            with (
                patch.object(f, "before_entry", side_effect=RuntimeError("crash")),
                self.assertRaises(RuntimeError),
            ):
                f.invoke()
            rec = parse_session_comment(f.comment["body"], identity=IDENTITY)
            assert rec is not None
            acc = ApiRunAccounting.from_document(rec.api_run_accounting)
            clean_blocks = tuple(replace(b, pending=()) for b in acc.blocks)
            clean_acc = replace(
                acc,
                blocks=clean_blocks,
                completed=(),
                analysis_abort=None,
                terminal_transaction=None,
            )
            payload = rec.to_dict()
            del payload["schema_version"], payload["record_sha256"]
            payload["transaction"] = None
            payload["reservation_id"] = None
            payload["reserved_slot"] = None
            payload["publication_staging"] = None
            payload["failed_attempts"] = 0
            payload["api_run_accounting"] = clean_acc.to_document()
            crashed_rec = SessionRecord._construct(**payload)

            self.assertTrue(is_startup_crash_predecessor(crashed_rec, clean_acc))

            # Extra / non-initial block is rejected even when pending=() and confirmed==0.
            extra_acc = clean_acc.reserve("extra-block", 1)
            self.assertFalse(is_startup_crash_predecessor(crashed_rec, extra_acc))
            non_initial_only_acc = replace(
                clean_acc,
                blocks=(
                    CallerRequestBlock(
                        block_id="successor-review",
                        issued_by=clean_acc.owner,
                        undispatched=1,
                        pending=(),
                    ),
                ),
            )
            self.assertFalse(
                is_startup_crash_predecessor(crashed_rec, non_initial_only_acc)
            )

            # Block issued by another owner is rejected.
            other_owner = replace(clean_acc.owner, job="other-job")
            foreign_owner_acc = replace(
                clean_acc,
                blocks=(
                    CallerRequestBlock(
                        block_id="hosted-review",
                        issued_by=other_owner,
                        undispatched=1,
                        pending=(),
                    ),
                ),
            )
            self.assertFalse(
                is_startup_crash_predecessor(crashed_rec, foreign_owner_acc)
            )

            # Non-zero prior_nonstartup_liability is rejected.
            prior_liability_acc = replace(clean_acc, prior_nonstartup_liability=1)
            self.assertFalse(
                is_startup_crash_predecessor(crashed_rec, prior_liability_acc)
            )


if __name__ == "__main__":
    unittest.main()
