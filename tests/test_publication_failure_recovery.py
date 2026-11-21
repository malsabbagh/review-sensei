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


def _witnesses_from_extra(extra_comments: dict[int, dict[str, object]]) -> list[tuple[int, dict[str, object], PreExternalWriteSettlementWitness]]:
    found: list[tuple[int, dict[str, object], PreExternalWriteSettlementWitness]] = []
    for cid, row in sorted(extra_comments.items()):
        parsed = parse_settlement_witness_comment(row.get("body"))
        if parsed is not None:
            found.append((cid, row, parsed))
    return found


class PublicationFailureRecoveryFixture(CommandEntryFixture):
    """Command+review fixture serving head-filtered check-runs and witness writes."""

    def __init__(self, directory: str, *, limit: int = 512) -> None:
        super().__init__(directory, limit=limit)
        self.truncate_check_runs_census = False
        self.witness_post_fault: str | None = None
        self.fail_publication_failed_patch: str | None = None
        self.fail_publication_failed_patch_skip: int = 0
        self.fail_finish_patch: str | None = None

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
            cid = 2_000_000 + len(self.extra_comments) + 1
            row = {
                "id": cid,
                "body": payload["body"],
                "user": {"type": "Bot", "login": "reviewsensei[bot]", "id": 999},
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
    def _blocking_and_advisory_round1(self, directory: str, *, limit: int = 512) -> PublicationFailureRecoveryFixture:
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
            raise GitHubPublicationError("synthetic pure publication crash before write")

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
            any(
                m in {"POST", "PATCH"} and "/issues" in path
                for _, m, path in f.trace
            )
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
            any(
                m in {"POST", "PATCH"} and "/issues" in path
                for _, m, path in f.trace
            )
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
                raise ReviewInputError("synthetic pure pre-checkpoint validation refusal")

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
                raise ReviewInputError("synthetic round-1 pre-checkpoint validation refusal")

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
                raise GitHubPublicationError("synthetic failure after external write started")

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

        # 2b) Lost witness POST response: absent -> missing witness -> next command AND next push hold
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


if __name__ == "__main__":
    unittest.main()
