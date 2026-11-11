"""Qualification tests for G08: command/event concurrency and barrier interleavings."""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request

from fake_github_http import json_response, make_http
from review_sensei_github.api_run_history import read_action_result, read_previous_run
from review_sensei_github.api_run_owner import (
    ApiRunActionsEvidence,
    claim_review_startup,
)
from review_sensei_github.api_run_requests import (
    ApiRunRequestTransport,
    ApiRunSessionLedger,
)
from review_sensei_github.http import GitHubHttp
from review_sensei_github.session_ledger import (
    GitHubIssueCommentSessionLedger,
    parse_session_comment,
    render_session_comment,
)
from test_api_work_lifecycle import LifecycleFixture
from test_api_work_persistence import BASE, HEAD, IDENTITY

from review_sensei.api_work import (
    ApiExecutionOwner,
    ApiRunIdentity,
)
from review_sensei.errors import ReviewInputError
from review_sensei.models import ReviewTransaction
from review_sensei.recovery_authority import Access, AuthorityRefusal, Funding
from review_sensei.session import SessionIdentity, SessionRecord
from review_sensei.writer_containment import (
    ContainedForwardingCoordinator,
    WireDispatchOutcome,
    WriterContainmentPosture,
)

try:
    from session_fixture_clock import SESSION_FIXTURE_NOW
except ModuleNotFoundError:
    from tests.session_fixture_clock import SESSION_FIXTURE_NOW


FIXED_NOW = SESSION_FIXTURE_NOW
LIFECYCLE_BARRIERS = (
    "admission",
    "reservation",
    "physical_dispatch",
    "result_retention",
    "terminal_write",
)


class BarrierCoordinator:
    """Deterministic barrier and single-writer invariant recorder for concurrency tests."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.entered = {name: threading.Event() for name in LIFECYCLE_BARRIERS}
        self.released = {name: threading.Event() for name in LIFECYCLE_BARRIERS}
        for event in self.released.values():
            event.set()
        self.active_writers: set[str] = set()
        self.max_concurrent_writers = 0
        self.paid_dispatches: list[tuple[str, str]] = []
        self.timeline: list[tuple[str, str]] = []

    def arm(self, barrier_name: str) -> None:
        assert barrier_name in LIFECYCLE_BARRIERS
        self.entered[barrier_name].clear()
        self.released[barrier_name].clear()

    def release(self, barrier_name: str) -> None:
        assert barrier_name in LIFECYCLE_BARRIERS
        self.released[barrier_name].set()

    def release_all(self) -> None:
        for event in self.released.values():
            event.set()

    def arrive(self, actor: str, barrier_name: str) -> None:
        assert barrier_name in LIFECYCLE_BARRIERS
        with self._lock:
            self.timeline.append((actor, barrier_name))
        self.entered[barrier_name].set()
        if not self.released[barrier_name].wait(timeout=5):
            raise TimeoutError(f"{actor} timed out at barrier {barrier_name}")

    def enter_writer(self, writer_id: str) -> None:
        with self._lock:
            self.active_writers.add(writer_id)
            if len(self.active_writers) > self.max_concurrent_writers:
                self.max_concurrent_writers = len(self.active_writers)

    def exit_writer(self, writer_id: str) -> None:
        with self._lock:
            self.active_writers.discard(writer_id)

    def record_paid_dispatch(self, writer_id: str, operation_key: str) -> None:
        with self._lock:
            self.paid_dispatches.append((writer_id, operation_key))


class CommandEventConcurrencyG08Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.coordinator = BarrierCoordinator()
        self.addCleanup(self.coordinator.release_all)

    def _start_initial_run(
        self, directory: Path, *, limit: int = 1024
    ) -> tuple[LifecycleFixture, ApiRunSessionLedger]:
        directory.mkdir(parents=True, exist_ok=True)
        f = LifecycleFixture(str(directory), limit=limit)
        native, configuration, _evidence, owner = f.prelude()
        f.accounted.initialize_and_activate(native, IDENTITY, configuration, owner)
        ledger = ApiRunSessionLedger(native, f.accounted, IDENTITY)
        return f, ledger

    def _new_run_caller(
        self,
        f: LifecycleFixture,
        directory: Path,
        *,
        run_id: int,
        job: str = "hosted",
    ) -> tuple[ApiRunRequestTransport, GitHubIssueCommentSessionLedger]:
        run_dir = directory / f"run-{run_id}-{job}"
        run_dir.mkdir(parents=True, exist_ok=True)
        caller = ApiRunRequestTransport(
            directory=run_dir,
            run=ApiRunIdentity(123, run_id),
            plan=f.plan,
            attempt=1,
            job=job,
            opener=f._external_open,
        )
        http = GitHubHttp(api_url="https://api.github.test", opener=caller.open)
        native = GitHubIssueCommentSessionLedger(
            http, token="synthetic", app_slug="reviewsensei[bot]"
        )
        return caller, native

    def _join_cleanly(self, thread: threading.Thread, barrier_name: str) -> None:
        self.coordinator.release(barrier_name)
        thread.join(timeout=5)
        self.assertFalse(
            thread.is_alive(),
            f"background worker thread leaked at barrier {barrier_name}",
        )

    def _new_readonly_contender_caller(
        self,
        f: LifecycleFixture,
        directory: Path,
        *,
        run_id: int,
        job: str = "hosted",
    ) -> tuple[ApiRunRequestTransport, GitHubIssueCommentSessionLedger]:
        """Create an isolated contender transport that reads comment snapshots without mutating ``f``."""
        run_dir = directory / f"contender-{run_id}-{job}"
        run_dir.mkdir(parents=True, exist_ok=True)
        comment_snapshot = copy.deepcopy(f.comment)
        extra_snapshot = copy.deepcopy(f.extra_comments)

        def snapshot_open(request: Request, timeout: float = 5.0):
            target = urlsplit(request.full_url)
            path = target.path
            bot = {"id": 999, "login": "reviewsensei[bot]", "type": "Bot"}
            if path == "/user":
                return json_response(bot)
            if path.endswith("/issues/7/comments") and request.method == "GET":
                query = parse_qs(target.query)
                size = int(query.get("per_page", [100])[0])
                page = int(query.get("page", [1])[0])
                rows = ([comment_snapshot] if comment_snapshot else []) + list(
                    extra_snapshot.values()
                )
                return json_response(
                    copy.deepcopy(rows[(page - 1) * size : page * size])
                )
            if path.endswith("/issues/comments/1000000") and request.method == "GET":
                return json_response(copy.deepcopy(comment_snapshot))
            raise AssertionError(f"unexpected contender HTTP {request.method} {path}")

        caller = ApiRunRequestTransport(
            directory=run_dir,
            run=ApiRunIdentity(123, run_id),
            plan=f.plan,
            attempt=1,
            job=job,
            opener=snapshot_open,
        )
        http = GitHubHttp(api_url="https://api.github.test", opener=caller.open)
        native = GitHubIssueCommentSessionLedger(
            http, token="synthetic", app_slug="reviewsensei[bot]"
        )
        return caller, native

    def test_a_review_vs_command_interleaving_across_barriers_and_recovery(
        self,
    ) -> None:
        """(a) Review vs command interleaving across all 5 barriers + deterministic recovery."""

        f, review_ledger = self._start_initial_run(self.dir / "case-a")
        config = f.accounted.accounting.configuration

        self.coordinator.arm("physical_dispatch")
        review_errors: list[Exception] = []

        def run_hosted_review() -> None:
            self.coordinator.enter_writer("run-456-hosted")
            try:
                self.coordinator.arrive("run-456-hosted", "admission")
                tx = ReviewTransaction.create(
                    repository=IDENTITY.repository,
                    pull_request=IDENTITY.pull_request,
                    base_sha=BASE,
                    head_sha=HEAD,
                    policy_digest="e" * 64,
                    configuration_digest=config.digest,
                    evidence_digest="a" * 64,
                    reservation_id="b" * 64,
                    generation=1,
                )
                self.coordinator.arrive("run-456-hosted", "reservation")
                review_ledger.replace(
                    IDENTITY,
                    lambda rec: rec.evolve(
                        generation=rec.generation + 1,
                        reservation_id=tx.reservation_id,
                        reserved_slot="initial",
                        transaction=tx,
                    ),
                )
                self.coordinator.arrive("run-456-hosted", "physical_dispatch")
                self.coordinator.record_paid_dispatch(
                    "run-456-hosted", f"review:{HEAD}:round1"
                )
                published_tx = replace(
                    tx,
                    phase="publication_succeeded",
                    result_sha256="b" * 64,
                )
                self.coordinator.arrive("run-456-hosted", "result_retention")
                review_ledger.claim_publication_completion(
                    IDENTITY, published_tx, published=True
                )
                published = review_ledger.replace(
                    IDENTITY,
                    lambda rec: rec.evolve(
                        generation=rec.generation + 1,
                        reservation_id=None,
                        reserved_slot=None,
                        transaction=published_tx,
                        last_committed_reservation_id=tx.reservation_id,
                    ),
                )
                self.coordinator.arrive("run-456-hosted", "terminal_write")
                f.accounted.finish_review_run(
                    review_ledger, IDENTITY, published.transaction
                )
            except Exception as exc:  # pragma: no cover - asserted empty
                review_errors.append(exc)
            finally:
                self.coordinator.exit_writer("run-456-hosted")

        review_thread = threading.Thread(target=run_hosted_review)
        review_thread.start()
        try:
            self.assertTrue(
                self.coordinator.entered["physical_dispatch"].wait(timeout=5)
            )

            # While hosted review is paused at physical_dispatch:
            # 1. Cross-run command sees active predecessor run 456 and refuses rollover.
            active_routes = [
                (
                    lambda req: urlsplit(req.full_url).path.endswith(
                        "/actions/runs/456"
                    ),
                    lambda _req: json_response(
                        {
                            "id": 456,
                            "run_attempt": 1,
                            "status": "in_progress",
                            "repository": {"id": 123, "full_name": "owner/repo"},
                        }
                    ),
                )
            ]
            ev_http, _ = make_http([], routes=active_routes)
            cmd_evidence = ApiRunActionsEvidence(
                ev_http, token="actions-read", command_run_id="457", job="command"
            )
            with self.assertRaisesRegex(
                ReviewInputError, "previous Actions run has not ceased"
            ):
                cmd_evidence.ceased_run_owner(
                    IDENTITY, 456, ApiExecutionOwner(1, "hosted", 0)
                )

            # 2. Direct action admission against an active review reservation is also refused.
            with self.assertRaisesRegex(
                ReviewInputError, "caller action conflicts with a review reservation"
            ):
                f.accounted.admit_action(
                    review_ledger,
                    IDENTITY,
                    purpose="command",
                    source={
                        "kind": "issue_comment",
                        "id": 51,
                        "updated_at": "2026-10-25T12:00:00Z",
                        "body_sha256": "1" * 64,
                    },
                    head_sha=HEAD,
                    operation_key="2" * 64,
                )
        finally:
            # Release hosted review to complete cleanly.
            self._join_cleanly(review_thread, "physical_dispatch")
        self.assertEqual(review_errors, [])

        # Now Run 457 (hosted/command rollover) succeeds deterministically and executes command!
        review_root = parse_session_comment(f.comment["body"], identity=IDENTITY)
        assert review_root is not None
        cmd_caller, cmd_native = self._new_run_caller(
            f, self.dir / "case-a", run_id=457, job="hosted"
        )
        f.trace = []
        f.accounted = cmd_caller
        self.coordinator.enter_writer("run-457-command")
        try:
            cmd_native.load(IDENTITY)
            cmd_caller.rollover_and_activate(
                cmd_native,
                IDENTITY,
                config,
                ApiExecutionOwner(1, "hosted", 0),
                review_root,
                lambda: None,
            )
            cmd_ledger = ApiRunSessionLedger(cmd_native, cmd_caller, IDENTITY)
            intent = cmd_caller.admit_action(
                cmd_ledger,
                IDENTITY,
                purpose="command",
                source={
                    "kind": "issue_comment",
                    "id": 51,
                    "updated_at": "2026-10-25T12:00:00Z",
                    "body_sha256": "1" * 64,
                },
                head_sha=HEAD,
                operation_key="2" * 64,
            )
            boundary = cmd_caller.begin_action_effect(cmd_ledger, IDENTITY, intent)
            cmd_ledger.replace(
                IDENTITY,
                lambda rec: rec.evolve(
                    generation=rec.generation + 1, operator_paused=True
                ),
            )
            proof = cmd_caller.settle_action_effect(
                cmd_ledger,
                IDENTITY,
                boundary,
                outcome={"kind": "applied", "reason": None},
                result_document={"command": "pause", "applied": True},
            )
            cmd_root = cmd_caller.close_action(cmd_ledger, IDENTITY, proof)
        finally:
            self.coordinator.exit_writer("run-457-command")

        self.assertTrue(cmd_root.operator_paused)
        self.assertEqual(self.coordinator.max_concurrent_writers, 1)
        self.assertEqual(
            len(self.coordinator.paid_dispatches),
            len(set(self.coordinator.paid_dispatches)),
        )

        # Subsequent review Run 458 rolls over cleanly after command completion (zero deadlock).
        rev2_caller, rev2_native = self._new_run_caller(
            f, self.dir / "case-a", run_id=458, job="hosted"
        )
        f.trace = []
        f.accounted = rev2_caller
        rev2_native.load(IDENTITY)
        rev2_caller.rollover_and_activate(
            rev2_native,
            IDENTITY,
            config,
            ApiExecutionOwner(1, "hosted", 0),
            cmd_root,
            lambda: None,
        )
        self.assertEqual(rev2_caller.accounting.identity.run_id, 458)

    def test_b_reply_vs_command_interleaving_and_archive_continuity(self) -> None:
        """(b) Reply vs command interleaving, same-slot lock, and post-reply recovery."""

        f, reply_ledger = self._start_initial_run(self.dir / "case-b")
        config = f.accounted.accounting.configuration

        # Same-slot duplicate startup claim is deterministically rejected.
        slot_dir = self.dir / "case-b-slot"
        slot_dir.mkdir()
        claim_review_startup(slot_dir, ApiRunIdentity(123, 456), 1, "command")
        with self.assertRaisesRegex(
            ReviewInputError, "API startup slot is consumed or unavailable"
        ):
            claim_review_startup(slot_dir, ApiRunIdentity(123, 456), 1, "command")

        self.coordinator.arm("result_retention")
        reply_errors: list[Exception] = []
        reply_holders: dict[str, SessionRecord] = {}

        def run_reply_worker() -> None:
            self.coordinator.enter_writer("run-456-reply")
            try:
                intent = f.accounted.admit_action(
                    reply_ledger,
                    IDENTITY,
                    purpose="reply",
                    source={
                        "kind": "review_comment",
                        "id": 88,
                        "updated_at": "2026-10-25T12:05:00Z",
                        "body_sha256": "3" * 64,
                    },
                    head_sha=HEAD,
                    operation_key="4" * 64,
                )
                boundary = f.accounted.begin_action_effect(
                    reply_ledger, IDENTITY, intent
                )
                self.coordinator.record_paid_dispatch("run-456-reply", "reply:88")
                self.coordinator.arrive("run-456-reply", "result_retention")
                proof = f.accounted.settle_action_effect(
                    reply_ledger,
                    IDENTITY,
                    boundary,
                    outcome={"kind": "applied", "reason": None},
                    result_document={"reply_comment_id": 9001},
                )
                reply_holders["root"] = f.accounted.close_action(
                    reply_ledger, IDENTITY, proof
                )
            except Exception as exc:  # pragma: no cover - asserted empty
                reply_errors.append(exc)
            finally:
                self.coordinator.exit_writer("run-456-reply")

        worker = threading.Thread(target=run_reply_worker)
        worker.start()
        try:
            self.assertTrue(
                self.coordinator.entered["result_retention"].wait(timeout=5)
            )

            # Concurrent command run attempting rollover while reply is pending fails closed
            # without mutating ``f.accounted`` or ``f.trace`` while ``worker`` is live.
            mid_root = parse_session_comment(f.comment["body"], identity=IDENTITY)
            assert mid_root is not None
            early_caller, early_native = self._new_readonly_contender_caller(
                f, self.dir / "case-b-early", run_id=457, job="hosted"
            )
            early_native.load(IDENTITY)
            with self.assertRaisesRegex(
                ReviewInputError, "previous caller run is not terminal"
            ):
                early_caller.rollover_and_activate(
                    early_native,
                    IDENTITY,
                    config,
                    ApiExecutionOwner(1, "hosted", 0),
                    mid_root,
                    lambda: None,
                )
        finally:
            # Release reply to finish terminal write; command run 458 then rolls over cleanly.
            self._join_cleanly(worker, "result_retention")
        self.assertEqual(reply_errors, [])
        reply_root = reply_holders["root"]

        cmd_caller, cmd_native = self._new_run_caller(
            f, self.dir / "case-b-cmd", run_id=458, job="hosted"
        )
        f.trace = []
        f.accounted = cmd_caller
        cmd_native.load(IDENTITY)
        cmd_caller.rollover_and_activate(
            cmd_native,
            IDENTITY,
            config,
            ApiExecutionOwner(1, "hosted", 0),
            reply_root,
            lambda: None,
        )
        cmd_ledger = ApiRunSessionLedger(cmd_native, cmd_caller, IDENTITY)
        archived_reply = read_previous_run(
            cmd_ledger, IDENTITY, cmd_caller.accounting.previous_record
        )
        self.assertEqual(
            read_action_result(cmd_ledger, IDENTITY, archived_reply)["result"],
            {"reply_comment_id": 9001},
        )
        self.assertEqual(self.coordinator.max_concurrent_writers, 1)
        self.assertEqual(
            self.coordinator.paid_dispatches, [("run-456-reply", "reply:88")]
        )

    def test_c_local_vs_hosted_exclusivity_sibling_cessation_and_handover(
        self,
    ) -> None:
        """(c) Local vs hosted writer overlap refusal and sibling-job cessation tightening."""

        state = {
            "run_status": "in_progress",
            "hosted_status": "in_progress",
            "hosted_conclusion": None,
            "local_status": "in_progress",
            "local_conclusion": None,
            "command_status": "completed",
            "command_conclusion": "skipped",
        }

        def routes():
            return [
                (
                    lambda req: urlsplit(req.full_url).path.endswith(
                        "/actions/runs/500"
                    ),
                    lambda _req: json_response(
                        {
                            "id": 500,
                            "run_attempt": 1,
                            "status": state["run_status"],
                            "event": "pull_request",
                            "repository": {"id": 123, "full_name": "owner/repo"},
                        }
                    ),
                ),
                (
                    lambda req: "/attempts/1/jobs" in urlsplit(req.full_url).path,
                    lambda _req: json_response(
                        {
                            "total_count": 3,
                            "jobs": [
                                {
                                    "id": 5001,
                                    "run_id": 500,
                                    "run_attempt": 1,
                                    "name": "ReviewSensei / hosted",
                                    "status": state["hosted_status"],
                                    "conclusion": state["hosted_conclusion"],
                                    "completed_at": (
                                        "2026-10-26T00:05:00Z"
                                        if state["hosted_status"] == "completed"
                                        else None
                                    ),
                                },
                                {
                                    "id": 5002,
                                    "run_id": 500,
                                    "run_attempt": 1,
                                    "name": "ReviewSensei / local",
                                    "status": state["local_status"],
                                    "conclusion": state["local_conclusion"],
                                    "completed_at": (
                                        "2026-10-26T00:06:00Z"
                                        if state["local_status"] == "completed"
                                        else None
                                    ),
                                },
                                {
                                    "id": 5003,
                                    "run_id": 500,
                                    "run_attempt": 1,
                                    "name": "ReviewSensei / command",
                                    "status": state["command_status"],
                                    "conclusion": state["command_conclusion"],
                                    "completed_at": (
                                        "2026-10-26T00:04:00Z"
                                        if state["command_status"] == "completed"
                                        else None
                                    ),
                                },
                            ],
                        }
                    ),
                ),
            ]

        http, _ = make_http([], routes=routes())
        hosted_ev = ApiRunActionsEvidence(
            http, token="actions-read", command_run_id="500", job="hosted"
        )
        local_ev = ApiRunActionsEvidence(
            http, token="actions-read", command_run_id="500", job="local"
        )

        # 1. Both in_progress -> both writers are refused (zero ambiguous ownership).
        for ev in (hosted_ev, local_ev):
            with self.assertRaisesRegex(
                ReviewInputError, "API writer ownership is ambiguous or overlapping"
            ):
                ev.current_owner(IDENTITY, attempt=1, generation=0)

        # 2. Hosted completed while sibling local is in_progress, queued, waiting, or
        #    completed without a terminal conclusion -> ceased_owner refuses and names the job!
        state["hosted_status"] = "completed"
        state["hosted_conclusion"] = "success"
        for sibling_status, sibling_conclusion in (
            ("in_progress", None),
            ("queued", None),
            ("waiting", None),
            ("completed", None),
        ):
            state["local_status"] = sibling_status
            state["local_conclusion"] = sibling_conclusion
            with self.assertRaises(ReviewInputError) as err_ctx:
                hosted_ev.ceased_owner(IDENTITY, ApiExecutionOwner(1, "hosted", 0))
            self.assertIn("API predecessor has not ceased", str(err_ctx.exception))
            self.assertIn(
                f"ReviewSensei / local={sibling_status}/{sibling_conclusion}",
                str(err_ctx.exception),
            )

        # 3. Conditional sibling jobs that never ran on GitHub Actions report
        #    status="completed" with conclusion="skipped" (and cancelled siblings report
        #    status="completed" with conclusion="cancelled"): both permit clean handover!
        state["run_status"] = "completed"
        for terminal_sibling_conclusion in ("skipped", "cancelled"):
            state["local_status"] = "completed"
            state["local_conclusion"] = terminal_sibling_conclusion
            job_id, stamp, conclusion = hosted_ev.ceased_owner(
                IDENTITY, ApiExecutionOwner(1, "hosted", 0)
            )
            self.assertEqual(
                (job_id, stamp, conclusion), (5001, "2026-10-26T00:05:00Z", "success")
            )
            successor_ev = ApiRunActionsEvidence(
                http, token="actions-read", command_run_id="501", job="local"
            )
            self.assertEqual(
                successor_ev.ceased_run_owner(
                    IDENTITY, 500, ApiExecutionOwner(1, "hosted", 0)
                )[0],
                500,
            )

    def test_d_cancelled_worker_delayed_write_fencing_and_unknown_preservation(
        self,
    ) -> None:
        """(d) Cancellation != cessation, preserved unknown effect, and fenced delayed PATCH."""

        # 1. Cancel requested while status=in_progress is NOT cessation.
        cancel_routes = [
            (
                lambda req: urlsplit(req.full_url).path.endswith("/actions/runs/600"),
                lambda _req: json_response(
                    {
                        "id": 600,
                        "run_attempt": 1,
                        "status": "in_progress",
                        "conclusion": None,
                        "repository": {"id": 123, "full_name": "owner/repo"},
                    }
                ),
            ),
            (
                lambda req: "/attempts/1/jobs" in urlsplit(req.full_url).path,
                lambda _req: json_response(
                    {
                        "total_count": 1,
                        "jobs": [
                            {
                                "id": 6001,
                                "run_id": 600,
                                "run_attempt": 1,
                                "name": "ReviewSensei / hosted",
                                "status": "in_progress",
                                "conclusion": None,
                                "completed_at": None,
                            }
                        ],
                    }
                ),
            ),
        ]
        http, _ = make_http([], routes=cancel_routes)
        ev = ApiRunActionsEvidence(
            http, token="actions-read", command_run_id="601", job="hosted"
        )
        with self.assertRaisesRegex(
            ReviewInputError, "previous Actions run has not ceased"
        ):
            ev.ceased_run_owner(IDENTITY, 600, ApiExecutionOwner(1, "hosted", 0))

        # 2. Unknown mutation before cancellation preserves uncertain liability and blocks settlement.
        f, ledger = self._start_initial_run(self.dir / "case-d-unknown")
        intent = f.accounted.admit_action(
            ledger,
            IDENTITY,
            purpose="command",
            source={
                "kind": "issue_comment",
                "id": 61,
                "updated_at": "2026-10-25T12:10:00Z",
                "body_sha256": "5" * 64,
            },
            head_sha=HEAD,
            operation_key="6" * 64,
        )
        boundary = f.accounted.begin_action_effect(ledger, IDENTITY, intent)
        f.fault = "unknown-absent"
        with self.assertRaises(Exception):
            f.accounted.open(
                Request(
                    "https://api.github.test/repos/owner/repo/issues/comments/1000000",
                    data=json.dumps({"body": f.comment["body"]}).encode("utf-8"),
                    method="PATCH",
                ),
                timeout=5,
            )
        self.assertGreaterEqual(f.accounted.accounting.uncertain, 1)
        with self.assertRaisesRegex(ReviewInputError, "confirmed outcome"):
            f.accounted.settle_action_effect(
                ledger,
                IDENTITY,
                boundary,
                outcome={"kind": "applied", "reason": None},
                result_document={"applied": True},
            )

        # 3. Cancelled worker with delayed terminal_write PATCH is fenced by canonical authority,
        # allowing successor writer to advance cleanly without clobber.
        pr_id = SessionIdentity("example/repo", 7, repository_id=123)
        initial = SessionRecord.create(pr_id, now=FIXED_NOW).evolve(
            generation=1, now=FIXED_NOW
        )
        comment_state = {
            "body": render_session_comment(
                repository_id=123, pull_request=7, record=initial
            )
        }

        def wire(
            method: str, target: str, request_bytes: bytes, _binding: dict[str, object]
        ) -> WireDispatchOutcome:
            payload = json.loads(request_bytes.decode("utf-8"))
            comment_state["body"] = str(payload["body"])
            resp = json.dumps({"id": 701, "body": comment_state["body"]}).encode(
                "utf-8"
            )
            return WireDispatchOutcome(status_code=200, response_body=resp)

        containment = ContainedForwardingCoordinator(
            self.dir / "case-d-fenced",
            posture=WriterContainmentPosture.fully_contained(),
            wire_forwarder=wire,
            synthetic_qualification=True,
        )
        base_access = Access(
            repository="example/repo",
            repository_id=123,
            pull_request=7,
            store_id="a" * 64,
            epoch=1,
            caller_sha256="1" * 64,
            source_sha256="c" * 64,
            head_sha="d" * 40,
            configuration_sha256="e" * 64,
            run_id=600,
            attempt=1,
            actor_id=99,
            actor_role="admin",
            workflow_sha="5" * 40,
            policy_commit="6" * 40,
            funding=Funding("f" * 64, 512, 50, 100000, 0, 0, 0),
            native_exclusive=True,
        )
        tok1, _ = containment.enroll_initial_writer(
            base_access=base_access,
            writer_route="actions_hosted",
        )
        self.coordinator.arm("terminal_write")
        cancelled_errors: list[Exception] = []

        def cancelled_worker() -> None:
            try:
                self.coordinator.arrive("cancelled-epoch1", "terminal_write")
                stale = initial.evolve(
                    generation=2, operator_paused=True, now=FIXED_NOW
                )
                containment.fenced_http_mutation(
                    tok1,
                    dispatch_id="cancelled-late-patch",
                    operation="session_patch",
                    method="PATCH",
                    target="/repos/example/repo/issues/comments/701",
                    payload={
                        "body": render_session_comment(
                            repository_id=123, pull_request=7, record=stale
                        )
                    },
                    reservation_sha256="7" * 64,
                )
            except Exception as exc:
                cancelled_errors.append(exc)

        thread = threading.Thread(target=cancelled_worker)
        thread.start()
        try:
            self.assertTrue(self.coordinator.entered["terminal_write"].wait(timeout=5))

            tok2, _ = containment.advance_epoch(
                predecessor_token=tok1,
                successor_access=replace(
                    base_access, run_id=601, caller_sha256="2" * 64
                ),
                writer_route="actions_hosted",
                predecessor_ceased=True,
            )
            successor_rec = initial.evolve(
                generation=2, operator_paused=False, now=FIXED_NOW
            )
            containment.fenced_http_mutation(
                tok2,
                dispatch_id="successor-clean-patch",
                operation="session_patch",
                method="PATCH",
                target="/repos/example/repo/issues/comments/701",
                payload={
                    "body": render_session_comment(
                        repository_id=123, pull_request=7, record=successor_rec
                    )
                },
                reservation_sha256="8" * 64,
            )
        finally:
            self._join_cleanly(thread, "terminal_write")

        self.assertEqual(len(cancelled_errors), 1)
        self.assertIsInstance(cancelled_errors[0], AuthorityRefusal)
        final_rec = parse_session_comment(comment_state["body"], identity=pr_id)
        assert final_rec is not None
        self.assertEqual(final_rec.generation, 2)
        self.assertFalse(final_rec.operator_paused)

    def test_e_queued_job_replacement_non_fifo_converges_without_lost_obligations(
        self,
    ) -> None:
        """(e) Queued jobs waking out of FIFO order converge to live SHA without lost state."""

        from types import SimpleNamespace

        from review_sensei_github.api_run_host import ApiRunReviewHooks
        from review_sensei_github.conversation import (
            ConversationPublisher,
            PreparedConversation,
        )

        sha1 = "1" * 40
        sha2 = "2" * 40
        sha3 = HEAD
        f, initial_ledger = self._start_initial_run(self.dir / "case-e")
        # Persist a durable operator obligation (operator_paused=True) before commit churn.
        intent = f.accounted.admit_action(
            initial_ledger,
            IDENTITY,
            purpose="command",
            source={
                "kind": "issue_comment",
                "id": 77,
                "updated_at": "2026-10-25T12:15:00Z",
                "body_sha256": "7" * 64,
            },
            head_sha=HEAD,
            operation_key="8" * 64,
        )
        boundary = f.accounted.begin_action_effect(initial_ledger, IDENTITY, intent)
        initial_ledger.replace(
            IDENTITY,
            lambda rec: rec.evolve(generation=rec.generation + 1, operator_paused=True),
        )
        proof = f.accounted.settle_action_effect(
            initial_ledger,
            IDENTITY,
            boundary,
            outcome={"kind": "applied", "reason": None},
            result_document={"operator_paused": True},
        )
        obligation_root = f.accounted.close_action(initial_ledger, IDENTITY, proof)
        config = f.accounted.accounting.configuration

        comment_routes = [
            (
                lambda req: urlsplit(req.full_url).path.endswith("/issues/comments/77"),
                lambda _req: json_response(
                    {
                        "id": 77,
                        "body": "@reviewsensei why did this trigger?",
                        "updated_at": "2026-10-25T12:15:00Z",
                        "issue_url": "https://api.github.test/repos/owner/repo/issues/7",
                        "author_association": "OWNER",
                        "user": {"id": 9, "login": "maint", "type": "User"},
                    }
                ),
            ),
            (
                lambda req: urlsplit(req.full_url).path.endswith("/pulls/7"),
                lambda _req: json_response(
                    {
                        "number": 7,
                        "state": "open",
                        "draft": False,
                        "title": "PR 7",
                        "body": "",
                        "head": {
                            "sha": sha3,
                            "ref": "feature",
                            "repo": {
                                "id": 123,
                                "full_name": "owner/repo",
                                "fork": False,
                            },
                        },
                        "base": {
                            "sha": BASE,
                            "ref": "main",
                            "repo": {
                                "id": 123,
                                "full_name": "owner/repo",
                                "fork": False,
                            },
                        },
                    }
                ),
            ),
        ]
        conv_http, _ = make_http([], routes=comment_routes)
        conversation = ConversationPublisher(http=conv_http)

        dispatched_shas: list[str] = []
        next_caller, next_native = self._new_run_caller(
            f, self.dir / "case-e", run_id=457, job="hosted"
        )
        f.trace = []
        f.accounted = next_caller
        next_native.load(IDENTITY)
        next_caller.rollover_and_activate(
            next_native,
            IDENTITY,
            config,
            ApiExecutionOwner(1, "hosted", 0),
            obligation_root,
            lambda: None,
        )
        next_ledger = ApiRunSessionLedger(next_native, next_caller, IDENTITY)

        policy_file = self.dir / "case-e" / "frozen-policy.yml"
        policy_file.write_bytes(f.config_bytes)
        args = SimpleNamespace(
            repository=IDENTITY.repository,
            pull_request=IDENTITY.pull_request,
            repository_id=IDENTITY.repository_id,
            base_sha=BASE,
            config=policy_file,
        )

        def attempt_queued_dispatch(candidate_sha: str) -> str:
            reply_check = conversation.prepare_context(
                token="synthetic",
                repository=IDENTITY.repository,
                pull_request=IDENTITY.pull_request,
                source_comment_id=77,
                source_updated_at="2026-10-25T12:15:00Z",
                app_slug="reviewsensei[bot]",
                source_kind="issue",
                expected_head_sha=candidate_sha,
                defer_evidence=True,
            )
            hooks = ApiRunReviewHooks(
                next_ledger,
                IDENTITY,
                base_sha=BASE,
                head_sha=candidate_sha,
            )
            try:
                admitted_ledger = hooks.resolve_ledger(args, resolved_head_sha=sha3)
            except ReviewInputError as exc:
                self.assertEqual(
                    getattr(reply_check, "status", None), "skipped_stale_head"
                )
                self.assertIn("caller hosted admission scope conflicts", str(exc))
                return "skipped_stale_head"
            self.assertIsInstance(reply_check, PreparedConversation)
            assert isinstance(reply_check, PreparedConversation)
            self.assertEqual(reply_check.head_sha, sha3)
            hooks.begin_provider_phase(admitted_ledger)
            self.coordinator.record_paid_dispatch(
                "run-457-hosted", f"review:{candidate_sha}"
            )
            dispatched_shas.append(candidate_sha)
            next_ledger.replace(
                IDENTITY,
                lambda cur: cur.evolve(generation=cur.generation + 1),
            )
            return "dispatched"

        # Non-FIFO wakeup order across queued jobs: SHA_2 (stale), SHA_3 (current), SHA_1 (late stale).
        outcomes = [
            attempt_queued_dispatch(candidate_sha)
            for candidate_sha in (sha2, sha3, sha1)
        ]
        self.assertEqual(
            outcomes,
            ["skipped_stale_head", "dispatched", "skipped_stale_head"],
        )

        converged = parse_session_comment(f.comment["body"], identity=IDENTITY)
        assert converged is not None
        self.assertEqual(dispatched_shas, [sha3])
        self.assertEqual(
            self.coordinator.paid_dispatches, [("run-457-hosted", f"review:{sha3}")]
        )
        # Prior operator obligation (operator_paused=True) and archived root survive non-FIFO churn!
        self.assertTrue(converged.operator_paused)
        archived = read_previous_run(
            next_ledger, IDENTITY, next_caller.accounting.previous_record
        )
        self.assertEqual(archived.record_sha256, obligation_root.record_sha256)

    def test_f_two_manual_commands_serialize_and_preserve_both_dispositions(
        self,
    ) -> None:
        """(f) Two manual commands racing at admission/terminal_write serialize cleanly."""

        f, cmd1_ledger = self._start_initial_run(self.dir / "case-f")
        config = f.accounted.accounting.configuration

        # Manual command #101 admits and pauses right before terminal_write;
        # concurrent manual command #102 attempts to mutate the same session comment.
        self.coordinator.arm("terminal_write")
        cmd1_holder: dict[str, SessionRecord] = {}
        cmd1_errors: list[Exception] = []

        def run_manual_command_1() -> None:
            self.coordinator.enter_writer("manual-cmd-101")
            try:
                intent1 = f.accounted.admit_action(
                    cmd1_ledger,
                    IDENTITY,
                    purpose="command",
                    source={
                        "kind": "issue_comment",
                        "id": 101,
                        "updated_at": "2026-10-25T13:00:00Z",
                        "body_sha256": hashlib.sha256(
                            b"/reviewsensei pause"
                        ).hexdigest(),
                    },
                    head_sha=HEAD,
                    operation_key="a" * 64,
                )
                boundary1 = f.accounted.begin_action_effect(
                    cmd1_ledger, IDENTITY, intent1
                )
                cmd1_ledger.replace(
                    IDENTITY,
                    lambda rec: rec.evolve(
                        generation=rec.generation + 1, operator_paused=True
                    ),
                )
                proof1 = f.accounted.settle_action_effect(
                    cmd1_ledger,
                    IDENTITY,
                    boundary1,
                    outcome={"kind": "applied", "reason": None},
                    result_document={"command_id": 101, "operator_paused": True},
                )
                self.coordinator.arrive("manual-cmd-101", "terminal_write")
                cmd1_holder["root"] = f.accounted.close_action(
                    cmd1_ledger, IDENTITY, proof1
                )
            except Exception as exc:  # pragma: no cover - asserted empty
                cmd1_errors.append(exc)
            finally:
                self.coordinator.exit_writer("manual-cmd-101")

        t1 = threading.Thread(target=run_manual_command_1)
        t1.start()
        try:
            self.assertTrue(self.coordinator.entered["terminal_write"].wait(timeout=5))

            # Concurrent manual command #102 arriving before #101 closes is refused cleanly
            # without mutating ``f.accounted`` or ``f.trace`` while ``t1`` is live.
            mid_record = parse_session_comment(f.comment["body"], identity=IDENTITY)
            assert mid_record is not None
            early_cmd2_caller, early_cmd2_native = self._new_readonly_contender_caller(
                f, self.dir / "case-f-early2", run_id=457, job="hosted"
            )
            early_cmd2_native.load(IDENTITY)
            with self.assertRaisesRegex(
                ReviewInputError, "previous caller run is not terminal"
            ):
                early_cmd2_caller.rollover_and_activate(
                    early_cmd2_native,
                    IDENTITY,
                    config,
                    ApiExecutionOwner(1, "hosted", 0),
                    mid_record,
                    lambda: None,
                )
        finally:
            # Release #101 to finish terminal_write.
            self._join_cleanly(t1, "terminal_write")
        self.assertEqual(cmd1_errors, [])
        cmd1_root = cmd1_holder["root"]
        self.assertTrue(cmd1_root.operator_paused)

        # Serialized manual command #102 (resume) now rolls over cleanly, archiving #101!
        cmd2_caller, cmd2_native = self._new_run_caller(
            f, self.dir / "case-f-cmd2", run_id=458, job="hosted"
        )
        f.trace = []
        f.accounted = cmd2_caller
        self.coordinator.enter_writer("manual-cmd-102")
        try:
            cmd2_native.load(IDENTITY)
            cmd2_caller.rollover_and_activate(
                cmd2_native,
                IDENTITY,
                config,
                ApiExecutionOwner(1, "hosted", 0),
                cmd1_root,
                lambda: None,
            )
            cmd2_ledger = ApiRunSessionLedger(cmd2_native, cmd2_caller, IDENTITY)
            intent2 = cmd2_caller.admit_action(
                cmd2_ledger,
                IDENTITY,
                purpose="command",
                source={
                    "kind": "issue_comment",
                    "id": 102,
                    "updated_at": "2026-10-25T13:05:00Z",
                    "body_sha256": hashlib.sha256(b"/reviewsensei resume").hexdigest(),
                },
                head_sha=HEAD,
                operation_key="b" * 64,
            )
            boundary2 = cmd2_caller.begin_action_effect(cmd2_ledger, IDENTITY, intent2)
            cmd2_ledger.replace(
                IDENTITY,
                lambda rec: rec.evolve(
                    generation=rec.generation + 1, operator_paused=False
                ),
            )
            proof2 = cmd2_caller.settle_action_effect(
                cmd2_ledger,
                IDENTITY,
                boundary2,
                outcome={"kind": "applied", "reason": None},
                result_document={"command_id": 102, "operator_paused": False},
            )
            cmd2_root = cmd2_caller.close_action(cmd2_ledger, IDENTITY, proof2)
        finally:
            self.coordinator.exit_writer("manual-cmd-102")

        self.assertFalse(cmd2_root.operator_paused)
        archived_cmd1 = read_previous_run(
            cmd2_ledger, IDENTITY, cmd2_caller.accounting.previous_record
        )
        self.assertTrue(archived_cmd1.operator_paused)
        self.assertEqual(
            read_action_result(cmd2_ledger, IDENTITY, archived_cmd1)["result"],
            {"command_id": 101, "operator_paused": True},
        )
        self.assertEqual(self.coordinator.max_concurrent_writers, 1)


if __name__ == "__main__":
    unittest.main()
