"""Cross-PR cessation, step-down paging, multi-page pricing, and reassessment key parity tests."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import tempfile
import unittest
from dataclasses import asdict, replace
from http.client import HTTPMessage
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from fake_github_http import json_response
from review_sensei_github.action_representability import (
    _command_pricing,
    guard_current_command_representability,
    require_action_settlement_representable,
)
from review_sensei_github.action_run import UnstartedCallerRecoveryGate
from review_sensei_github.api_run_owner import ApiRunActionsEvidence
from review_sensei_github.application import (
    GitHubApplication,
    GitHubWriteOptions,
    _RecordedHumanAssessmentReply,
)
from review_sensei_github.conversation import (
    ConversationPublisher,
    PreparedConversation,
    ReplyResult,
)
from review_sensei_github.http import MAX_GITHUB_RESPONSE_BYTES, GitHubHttp
from review_sensei_github.pre_entry_abort_recovery import (
    _detect_late_native_effects,
    _probe_owner_ceased,
    inspect_live_no_transaction_predecessor,
)
from review_sensei_github.publication import ReviewPublisher
from review_sensei_github.session_ledger import (
    parse_session_comment,
    render_session_comment,
)
from review_sensei_github.unstarted_action_recovery import (
    NoBusinessDispatchEvidence,
    authenticate_unstarted_cancellation,
)
from test_action_run import HEAD, CommandEntryFixture
from test_api_work_persistence import BASE, IDENTITY
from test_no_transaction_abort_g29 import _APP_AUTHOR, _base_accounting, _make_record
from test_p0_later_attempt_cessation import _attempt1_jobs, _skipped_step_job
from test_unstarted_action_recovery_g32 import (
    G32CommandEntryFixture,
    G32LifecycleFixture,
)

from review_sensei.api_work import ApiExecutionOwner
from review_sensei.bounded_evidence import EvidenceReadBudget
from review_sensei.disposition import FindingIdentity, MaintainerCommand
from review_sensei.errors import ReviewInputError
from review_sensei.human_assessment import (
    HumanAssessmentDecision,
    HumanAssessmentReply,
    HumanReviewFinding,
    PendingHumanReview,
)
from review_sensei.models import ConversationContext, ReviewTransaction


class _RawResponse:
    """Fake HTTP response emitting exact raw bytes so >512KiB triggers GitHubHTTPResponseTooLargeError."""

    def __init__(self, payload: object, status: int = 200) -> None:
        self.status = status
        raw = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode(
            "utf-8"
        )
        self._buf = io.BytesIO(raw)
        self.headers = HTTPMessage()
        self.headers["Content-Type"] = "application/json"

    def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)

    def __enter__(self) -> _RawResponse:
        return self

    def __exit__(self, *args: object) -> bool:
        return False


def _oversized_item_padding() -> str:
    # 35 KB per item ensures 20 items (~700 KB) > 512 KiB while 10 items (~350 KB) < 512 KiB.
    return "x" * 35_000


class PreEntryAbortCessationAndPagingTests(unittest.TestCase):
    """Task 1: `_probe_owner_ceased` #483 parity, `_run_reader` forwarding, and #488 step-down."""

    def test_probe_owner_ceased_direct_http_later_attempt_non_entry_and_controls(
        self,
    ) -> None:
        base_acc = _base_accounting(run_id=1001)
        state: dict[str, object] = {
            "latest_attempt": 2,
            "run_status": "completed",
            "run_conclusion": "failure",
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
                        "status": state["run_status"],
                        "conclusion": state["run_conclusion"],
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

        # 1. Later attempt 2..5 proving no business entry is accepted.
        self.assertTrue(_probe_owner_ceased(ledger, IDENTITY, base_acc, "tok"))

        state["latest_attempt"] = 5
        state["jobs"][3] = [  # type: ignore[index]
            {
                "id": 100103,
                "run_id": 1001,
                "run_attempt": 3,
                "name": "ReviewSensei / hosted",
                "status": "completed",
                "conclusion": "skipped",
            }
        ]
        state["jobs"][4] = [  # type: ignore[index]
            _skipped_step_job(
                job_id=100104,
                run_id=1001,
                attempt=4,
                job="command",
                step_name="Apply broker-attested maintainer command",
            )
        ]
        state["jobs"][5] = [  # type: ignore[index]
            _skipped_step_job(
                job_id=100105,
                run_id=1001,
                attempt=5,
                job="local",
                step_name="Run local review",
            ),
            {
                "id": 1001059,
                "run_id": 1001,
                "run_attempt": 5,
                "name": "aux-job",
                "status": "completed",
                "conclusion": "success",
            },
        ]
        self.assertTrue(_probe_owner_ceased(ledger, IDENTITY, base_acc, "tok"))

        # 2. Auxiliary job still running on later attempt fails closed (complete_terminal_inventory=True).
        state["jobs"][5][1]["status"] = "in_progress"  # type: ignore[index]
        state["jobs"][5][1]["conclusion"] = None  # type: ignore[index]
        self.assertFalse(_probe_owner_ceased(ledger, IDENTITY, base_acc, "tok"))
        state["jobs"][5][1]["status"] = "completed"  # type: ignore[index]
        state["jobs"][5][1]["conclusion"] = "success"  # type: ignore[index]

        # 3. Later attempt that entered writer step fails closed.
        state["jobs"][2][0]["steps"][1]["conclusion"] = "failure"  # type: ignore[index]
        self.assertFalse(_probe_owner_ceased(ledger, IDENTITY, base_acc, "tok"))
        state["jobs"][2][0]["steps"][1]["conclusion"] = "skipped"  # type: ignore[index]

        # 4. Exceeding MAX_LATER_ATTEMPT_INSPECTION (latest_attempt == 6) fails closed.
        state["latest_attempt"] = 6
        self.assertFalse(_probe_owner_ceased(ledger, IDENTITY, base_acc, "tok"))

        # 5. Rollback attempt (< predecessor.run_attempt) fails closed.
        state["latest_attempt"] = 1
        self.assertFalse(
            _probe_owner_ceased(
                ledger,
                IDENTITY,
                replace(
                    base_acc,
                    owner=ApiExecutionOwner(2, "hosted", 0),
                    startup_used=(("hosted", 1), ("hosted", 2)),
                ),
                "tok",
            )
        )

    def test_run_reader_ceased_run_owner_is_not_bypassed_for_complete_inventory(
        self,
    ) -> None:
        base_acc = _base_accounting(run_id=1001)
        recorded: list[tuple[object, ...]] = []

        class _DuckReader:
            command_run_id = "2002"

            def ceased_run_owner(
                self,
                identity,
                run_id,
                owner,
                *,
                complete_terminal_inventory: bool = False,
            ):
                recorded.append(("ceased_run_owner", complete_terminal_inventory))
                if not complete_terminal_inventory:
                    raise AssertionError("must pass complete_terminal_inventory=True")

            def ceased_owner(self, identity, owner):
                recorded.append(("ceased_owner",))
                raise AssertionError("must not bypass ceased_run_owner")

        ledger = SimpleNamespace(_run_reader=_DuckReader())
        self.assertTrue(_probe_owner_ceased(ledger, IDENTITY, base_acc, None))
        self.assertEqual(recorded, [("ceased_run_owner", True)])

    def test_pre_entry_abort_step_down_pagination_on_reviews_and_comments(self) -> None:
        base_acc = _base_accounting(run_id=1001)
        abort_tx = replace(
            ReviewTransaction.create(
                repository="owner/repo",
                pull_request=7,
                base_sha=BASE,
                head_sha=HEAD,
                reservation_id="5" * 64,
                policy_digest="2" * 64,
                configuration_digest="1" * 64,
                evidence_digest="3" * 64,
                generation=1,
            ),
            phase="analysis",
            result_sha256=None,
        )
        abort_acc = replace(
            base_acc,
            analysis_abort={
                "source_root": "1" * 64,
                "cleanup_root": "2" * 64,
                "transaction": abort_tx.to_dict(),
                "entry_state": "not_started",
                "run_id": 1001,
                "owner": asdict(base_acc.owner),
            },
        )
        abort_rec = _make_record(accounting=abort_acc, generation=2)
        session_body = render_session_comment(
            repository_id=123, pull_request=7, record=abort_rec
        )

        pad = _oversized_item_padding()
        comments = [
            {
                "id": 10 + i,
                "body": f"human note {i} {pad}",
                "user": {"type": "User", "login": "alice"},
            }
            for i in range(18)
        ] + [
            {
                "id": 99,
                "body": session_body,
                "user": _APP_AUTHOR,
                "issue_url": "https://api.github.test/repos/owner/repo/issues/7",
            }
        ]
        reviews = [
            {
                "id": 200 + i,
                "body": f"human review {i} {pad}",
                "commit_id": HEAD,
                "user": {"type": "User", "id": 111, "login": "human"},
            }
            for i in range(18)
        ]
        review_sizes: list[int] = []
        comment_sizes: list[int] = []

        def opener(request, timeout=None):
            _ = timeout
            target = urlsplit(request.full_url)
            path, query = target.path, parse_qs(target.query)
            if path.endswith("/issues/7/comments"):
                size = int(query["per_page"][0])
                page = int(query["page"][0])
                comment_sizes.append(size)
                return _RawResponse(comments[(page - 1) * size : page * size])
            if path.endswith("/pulls/7/reviews"):
                size = int(query["per_page"][0])
                page = int(query["page"][0])
                review_sizes.append(size)
                return _RawResponse(reviews[(page - 1) * size : page * size])
            if path == "/graphql":
                return json_response(
                    {
                        "data": {
                            "repository": {
                                "databaseId": 123,
                                "pullRequest": {
                                    "number": 7,
                                    "reviewThreads": {
                                        "totalCount": 0,
                                        "pageInfo": {
                                            "hasNextPage": False,
                                            "endCursor": None,
                                        },
                                        "nodes": [],
                                    },
                                },
                            }
                        }
                    }
                )
            if path.endswith("/actions/runs/1001"):
                return json_response(
                    {
                        "id": 1001,
                        "repository": {"id": 123, "full_name": "owner/repo"},
                        "status": "completed",
                        "conclusion": "failure",
                        "run_attempt": 1,
                    }
                )
            if path.endswith("/actions/runs/1001/attempts/1/jobs"):
                return json_response(
                    {
                        "total_count": 1,
                        "jobs": _attempt1_jobs(
                            1001, job="hosted", conclusion="failure"
                        ),
                    }
                )
            raise AssertionError(path)

        ledger = SimpleNamespace(
            http=GitHubHttp(api_url="https://api.github.test", opener=opener),
            token="tok",
            app_slug="reviewsensei[bot]",
            _evidence_author_id=999,
        )
        qual, comment_ref = inspect_live_no_transaction_predecessor(
            ledger=ledger, identity=IDENTITY, actions_token="tok"
        )
        self.assertTrue(qual.qualifies_rollover)
        self.assertEqual(comment_ref["id"], 99)
        # Step-down observed: 100 (>512 KiB) -> 20 (>512 KiB) -> 10 (fits!).
        self.assertEqual(comment_sizes, [100, 20, 10, 10])
        self.assertEqual(review_sizes, [100, 20, 10, 10])

        # Also exercise full step-down 100 -> 20 -> 10 -> 5 -> 1:
        # 4 items * 140 KB = 560 KB (>512 KiB) so per_page 100, 20, 10, 5 fail on page=1,
        # and per_page=1 succeeds in 4 retries + 5 page reads (4 items + EOF) = 9 <= 10.
        huge_pad_140 = "y" * 140_000
        huge_reviews = [
            {
                "id": 300 + i,
                "body": f"big {i} {huge_pad_140}",
                "commit_id": HEAD,
                "user": {"type": "User", "id": 111, "login": "human"},
            }
            for i in range(4)
        ]
        step_sizes: list[int] = []

        def step_all_opener(request, timeout=None):
            _ = timeout
            target = urlsplit(request.full_url)
            path, query = target.path, parse_qs(target.query)
            if path.endswith("/pulls/7/reviews"):
                size = int(query["per_page"][0])
                page = int(query["page"][0])
                step_sizes.append(size)
                return _RawResponse(huge_reviews[(page - 1) * size : page * size])
            if path == "/graphql":
                return json_response(
                    {
                        "data": {
                            "repository": {
                                "databaseId": 123,
                                "pullRequest": {
                                    "number": 7,
                                    "reviewThreads": {
                                        "totalCount": 0,
                                        "pageInfo": {
                                            "hasNextPage": False,
                                            "endCursor": None,
                                        },
                                        "nodes": [],
                                    },
                                },
                            }
                        }
                    }
                )
            raise AssertionError(path)

        step_ledger = SimpleNamespace(
            http=GitHubHttp(api_url="https://api.github.test", opener=step_all_opener),
            token="tok",
            app_slug="reviewsensei[bot]",
            _evidence_author_id=999,
        )
        self.assertIs(
            _detect_late_native_effects(step_ledger, IDENTITY, abort_rec, abort_acc),
            False,
        )
        self.assertEqual(step_sizes, [100, 20, 10, 5, 1, 1, 1, 1, 1])

        # Single item exceeding 512 KiB even at per_page=1 fails closed (`None`).
        too_big = [
            {
                "id": 400,
                "body": "z" * (MAX_GITHUB_RESPONSE_BYTES + 100),
                "commit_id": HEAD,
                "user": {"type": "User", "id": 111, "login": "human"},
            }
        ]
        step_sizes.clear()
        huge_reviews[:] = too_big
        self.assertIsNone(
            _detect_late_native_effects(step_ledger, IDENTITY, abort_rec, abort_acc)
        )
        self.assertEqual(step_sizes, [100, 20, 10, 5, 1])


class CrossRunG32CessationAndDeduplicationTests(unittest.TestCase):
    """Task 2: Cross-run G32 enforces complete_terminal_inventory=True and deduplicates cessation GETs."""

    def test_cross_run_g32_enforces_complete_terminal_inventory_true(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            f = G32LifecycleFixture(directory, limit=1024)
            native, configuration, _ev, owner = f.prelude()
            f.accounted.initialize_and_activate(native, IDENTITY, configuration, owner)
            from review_sensei_github.api_run_requests import ApiRunSessionLedger

            ledger = ApiRunSessionLedger(native, f.accounted, IDENTITY)
            f.accounted.admit_action(
                ledger,
                IDENTITY,
                purpose="command",
                source={
                    "kind": "issue_comment",
                    "id": 41,
                    "updated_at": "2026-10-25T12:00:00Z",
                    "body_sha256": hashlib.sha256(
                        b"@reviewsensei review pause"
                    ).hexdigest(),
                },
                head_sha=HEAD,
                operation_key="1" * 64,
            )
            stranded = parse_session_comment(f.comment["body"], identity=IDENTITY)
            assert stranded is not None
            ev_a1 = NoBusinessDispatchEvidence.from_stranded_caller(
                f.accounted, identity=IDENTITY, record=stranded
            )
            f.source_status = 404
            f.source_payload = {"message": "Not Found"}

            # Configure predecessor run 456 with a running auxiliary job alongside completed writer.
            f.run_id = 457
            f.attempt = 1
            f.trace = []
            run457_dir = Path(directory) / "run457"
            run457_dir.mkdir()
            from review_sensei_github.api_run_requests import ApiRunRequestTransport
            from review_sensei_github.session_ledger import (
                GitHubIssueCommentSessionLedger,
            )

            from review_sensei.api_work import ApiRunIdentity

            base_open = f._external_open

            def aux_open(request, timeout):
                path = urlsplit(request.full_url).path
                method = request.get_method()
                if method == "GET" and path == "/user":
                    return json_response(
                        {"id": 999, "login": "reviewsensei[bot]", "type": "Bot"}
                    )
                if method == "GET" and path.endswith("/issues/7/comments"):
                    return json_response([copy.deepcopy(f.comment)])
                if method == "GET" and path == "/repos/owner/repo/issues/comments/41":
                    return json_response({"message": "Not Found"}, status=404)
                if path == "/repos/owner/repo/actions/runs/456":
                    return json_response(
                        {
                            "id": 456,
                            "run_attempt": 1,
                            "status": "completed",
                            "conclusion": "failure",
                            "repository": {"id": 123, "full_name": "owner/repo"},
                        }
                    )
                if path == "/repos/owner/repo/actions/runs/456/attempts/1/jobs":
                    return json_response(
                        {
                            "total_count": 2,
                            "jobs": [
                                *_attempt1_jobs(
                                    456, job="hosted", conclusion="failure"
                                ),
                                {
                                    "id": 45699,
                                    "run_id": 456,
                                    "run_attempt": 1,
                                    "name": "auxiliary-cleanup",
                                    "status": "in_progress",
                                    "conclusion": None,
                                },
                            ],
                        }
                    )
                if path == "/repos/owner/repo/actions/runs/457":
                    return json_response(
                        {
                            "id": 457,
                            "run_attempt": 1,
                            "status": "in_progress",
                            "conclusion": None,
                            "repository": {"id": 123, "full_name": "owner/repo"},
                        }
                    )
                if path == "/repos/owner/repo/actions/runs/457/attempts/1/jobs":
                    return json_response(
                        {
                            "total_count": 1,
                            "jobs": [
                                {
                                    "id": 45701,
                                    "run_id": 457,
                                    "run_attempt": 1,
                                    "name": "ReviewSensei / hosted",
                                    "status": "in_progress",
                                    "conclusion": None,
                                }
                            ],
                        }
                    )
                return base_open(request, timeout)

            caller_457 = ApiRunRequestTransport(
                directory=run457_dir,
                run=ApiRunIdentity(123, 457),
                plan=f.plan,
                attempt=1,
                opener=aux_open,
            )
            http_457 = GitHubHttp(
                api_url="https://api.github.test", opener=caller_457.open
            )
            native_457 = GitHubIssueCommentSessionLedger(
                http_457, token="synthetic", app_slug="reviewsensei[bot]"
            )
            native_457.load(IDENTITY)
            ev_457 = ApiRunActionsEvidence(
                http_457,
                token="synthetic-actions",
                command_run_id="457",
                job="hosted",
            )
            with self.assertRaisesRegex(
                ReviewInputError,
                "pre-entry abort predecessor job inventory has not ceased",
            ):
                authenticate_unstarted_cancellation(
                    caller=caller_457,
                    ledger=native_457,
                    identity=IDENTITY,
                    record=stranded,
                    actions_evidence=ev_457,
                    no_business_dispatch_evidence=ev_a1,
                    mode="cross_run",
                )

    def test_cross_run_g32_rollover_deduplicates_later_attempt_cessation_gets(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fc = G32CommandEntryFixture(directory)
            self.assertEqual(fc.invoke(), 0, fc.error)

            fc.SOURCE_ID = 701
            fc.select_command("@reviewsensei review pause", new_run=True)

            def mutate_once(current: CommandEntryFixture) -> None:
                if current.source_reads == 1:
                    current.source["body"] = "edited before delete"
                    current.source["updated_at"] = "2026-10-26T00:05:00Z"

            fc.before_source_read = mutate_once
            self.assertNotEqual(fc.invoke_command(), 0)
            fc.before_source_read = None

            stranded_id = fc.run_id
            stranded_rec = parse_session_comment(fc.comment["body"], identity=IDENTITY)
            assert stranded_rec is not None
            dispatch_ev = NoBusinessDispatchEvidence.from_stranded_caller(
                fc.accounted, identity=IDENTITY, record=stranded_rec
            )

            # Configure predecessor run with K=4 skipped later attempts (attempt 1..5 -> 6 GETs/walk).
            orig_ext = fc.external
            prev_run_gets: list[str] = []

            def k4_ext(req, timeout):
                path = urlsplit(req.full_url).path
                if req.get_method() == "GET" and path.endswith("/issues/comments/701"):
                    fc._meter(req)
                    return json_response({"message": "Not Found"}, status=404)
                if req.get_method() == "GET" and path == (
                    f"/repos/owner/repo/actions/runs/{stranded_id}"
                ):
                    fc._meter(req)
                    prev_run_gets.append(path)
                    return json_response(
                        {
                            "id": stranded_id,
                            "run_attempt": 5,
                            "repository": {"id": 123, "full_name": "owner/repo"},
                            "status": "completed",
                            "conclusion": "failure",
                            "event": "issue_comment",
                        }
                    )
                if (
                    req.get_method() == "GET"
                    and path.startswith(
                        f"/repos/owner/repo/actions/runs/{stranded_id}/attempts/"
                    )
                    and path.endswith("/jobs")
                ):
                    fc._meter(req)
                    prev_run_gets.append(path)
                    att = int(path.split("/attempts/")[1].split("/")[0])
                    if att == 1:
                        rows = _attempt1_jobs(
                            stranded_id, job="command", conclusion="failure"
                        )
                    else:
                        rows = [
                            _skipped_step_job(
                                job_id=stranded_id * 100 + att,
                                run_id=stranded_id,
                                attempt=att,
                                job="command",
                                step_name="Apply broker-attested maintainer command",
                            )
                        ]
                    return json_response({"total_count": len(rows), "jobs": rows})
                return orig_ext(req, timeout)

            fc.external = k4_ext  # type: ignore[method-assign]
            fc.SOURCE_ID = 702
            fc.select_command("@reviewsensei review pause", new_run=True)
            cross_gate = UnstartedCallerRecoveryGate(
                target_run_id=stranded_id,
                no_business_dispatch_evidence=dispatch_ev,
                mode="cross_run",
                use_actions_evidence=True,
            )

            from test_unstarted_action_recovery_g32 import (
                OrdinaryCallerAuthorityGateG32Tests,
            )

            rc = OrdinaryCallerAuthorityGateG32Tests._invoke_with_gate(
                fc, unstarted_recovery=cross_gate
            )
            self.assertEqual(rc, 0, fc.error)
            # Exactly 2 walks (review_run initial stopped observation + unstarted_action_recovery
            # complete_terminal_inventory=True check) = 2 * (1 run + 5 attempts) = 12 GETs,
            # NOT 3 walks (18 GETs)!
            self.assertEqual(len(prev_run_gets), 12, prev_run_gets)

            # Negative control: TOCTOU between review_run initial observation (attempt=1)
            # and settle_prepare (attempt=2 skipped) STILL refuses fail-closed!
            toctou_dir = Path(directory) / "toctou"
            toctou_dir.mkdir()
            fc2 = G32CommandEntryFixture(str(toctou_dir))
            self.assertEqual(fc2.invoke(), 0, fc2.error)
            fc2.SOURCE_ID = 801
            fc2.select_command("@reviewsensei review pause", new_run=True)
            fc2.before_source_read = mutate_once
            self.assertNotEqual(fc2.invoke_command(), 0)
            fc2.before_source_read = None
            sid2 = fc2.run_id
            srec2 = parse_session_comment(fc2.comment["body"], identity=IDENTITY)
            assert srec2 is not None
            ev2 = NoBusinessDispatchEvidence.from_stranded_caller(
                fc2.accounted, identity=IDENTITY, record=srec2
            )
            orig_ext2 = fc2.external
            seen_run_reads = {"count": 0}

            def toctou_ext(req, timeout):
                path = urlsplit(req.full_url).path
                if req.get_method() == "GET" and path.endswith("/issues/comments/801"):
                    fc2._meter(req)
                    return json_response({"message": "Not Found"}, status=404)
                if req.get_method() == "GET" and path == (
                    f"/repos/owner/repo/actions/runs/{sid2}"
                ):
                    fc2._meter(req)
                    seen_run_reads["count"] += 1
                    att_now = 1 if seen_run_reads["count"] == 1 else 2
                    return json_response(
                        {
                            "id": sid2,
                            "run_attempt": att_now,
                            "repository": {"id": 123, "full_name": "owner/repo"},
                            "status": "completed",
                            "conclusion": "failure",
                            "event": "issue_comment",
                        }
                    )
                if (
                    req.get_method() == "GET"
                    and path.startswith(
                        f"/repos/owner/repo/actions/runs/{sid2}/attempts/"
                    )
                    and path.endswith("/jobs")
                ):
                    fc2._meter(req)
                    att = int(path.split("/attempts/")[1].split("/")[0])
                    rows = (
                        _attempt1_jobs(sid2, job="command", conclusion="failure")
                        if att == 1
                        else [
                            _skipped_step_job(
                                job_id=sid2 * 100 + att,
                                run_id=sid2,
                                attempt=att,
                                job="command",
                                step_name="Apply broker-attested maintainer command",
                            )
                        ]
                    )
                    return json_response({"total_count": len(rows), "jobs": rows})
                return orig_ext2(req, timeout)

            fc2.external = toctou_ext  # type: ignore[method-assign]
            fc2.SOURCE_ID = 802
            fc2.select_command("@reviewsensei review pause", new_run=True)
            gate2 = UnstartedCallerRecoveryGate(
                target_run_id=sid2,
                no_business_dispatch_evidence=ev2,
                mode="cross_run",
                use_actions_evidence=True,
            )
            rc2 = OrdinaryCallerAuthorityGateG32Tests._invoke_with_gate(
                fc2, unstarted_recovery=gate2
            )
            self.assertNotEqual(rc2, 0)
            self.assertIn("previous caller cessation changed", fc2.error)


class ActionRepresentabilityMultiPagePricingTests(unittest.TestCase):
    """Task 3: `_command_pricing` and entrypoints reflect `#488` multi-page overhead."""

    def test_command_pricing_explicit_multipage_and_default_preservation(self) -> None:
        caller = SimpleNamespace(
            accounting=SimpleNamespace(
                configuration=SimpleNamespace(limit=512),
                blocks=(),
            ),
            usable=512,
        )
        default_k1 = _command_pricing(
            caller, action="accept-risk", writes_enabled=True, finding_count=1
        )
        self.assertEqual(default_k1.business_mutation_requests, 10)
        self.assertEqual(default_k1.receipt_publication_requests, 8)
        self.assertEqual(default_k1.required_requests, 31)

        paged_k1 = _command_pricing(
            caller,
            action="accept-risk",
            writes_enabled=True,
            finding_count=1,
            receipt_pages=3,
            lifecycle_pages=2,
        )
        self.assertEqual(paged_k1.business_mutation_requests, 10 + 2 * (2 - 1))
        self.assertEqual(paged_k1.receipt_publication_requests, 6 + 2 * 3)
        self.assertEqual(paged_k1.required_requests, 31 + 2 * (3 - 1) + 2 * (2 - 1))

        # Status / writes_enabled=False stay zero regardless of page count.
        status_p = _command_pricing(
            caller,
            action="status",
            writes_enabled=True,
            receipt_pages=4,
            lifecycle_pages=3,
        )
        self.assertEqual(status_p.business_mutation_requests, 0)
        self.assertEqual(status_p.receipt_publication_requests, 0)

        with self.assertRaises(ReviewInputError):
            _command_pricing(
                caller, action="accept-risk", writes_enabled=True, receipt_pages=0
            )
        with self.assertRaises(ReviewInputError):
            _command_pricing(
                caller, action="accept-risk", writes_enabled=True, lifecycle_pages=0
            )

    def test_guard_and_settlement_entrypoints_observe_multipage_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = CommandEntryFixture(directory, limit=512)
            fixture.provider_comments = [
                {
                    "path": "src/app.py",
                    "line": 2,
                    "side": "RIGHT",
                    "body": "Blocking issue 0.",
                    "severity": "high",
                    "blocking": True,
                    "needs_human": False,
                }
            ]
            self.assertEqual(fixture.invoke(), 0, fixture.error)
            rec = parse_session_comment(fixture.comment["body"], identity=IDENTITY)
            assert rec is not None and rec.transaction is not None

            from review_sensei_github.publication import parse_finding_index

            index = parse_finding_index(
                fixture.reviews[-1]["body"],
                repository_id=123,
                pull_request=7,
                head_sha=HEAD,
            )
            assert index is not None
            ref = "RS-" + index.entries[0].instance[:6].upper()
            finding_inv = (
                FindingIdentity(
                    instance=index.entries[0].instance,
                    needs_human=True,
                    blocking=True,
                ),
            )

            # Seed cached comments so the PR sits above 512 KiB (forces multi-page lifecycle & receipt).
            pad = "a" * 34_000
            big_comments = [
                {
                    "id": 100 + i,
                    "body": f"comment-{i} {pad}",
                    "user": {"type": "User", "login": "alice"},
                }
                for i in range(16)
            ] + [copy.deepcopy(fixture.comment)]

            ledger = SimpleNamespace(
                _inventory_complete=True,
                _quarantine_required=False,
                _cached_comments=lambda: big_comments,
                _consumed_grants=(),
                _observed_consumed_grants=set(),
                _accepted_consumed_grant=None,
                evidence_producer=lambda: "github-bot:999",
            )
            cmd = MaintainerCommand(
                action="accept-risk",
                actor="maintainer",
                reason="acceptable risk",
                head_sha=HEAD,
                finding_references=(ref,),
            )

            # Base K=1 takes 31 calls; with multi-page (receipt_pages=2, lifecycle_pages=2)
            # it requires 35 calls -> usable=31 MUST refuse pre-effect, usable=40 passes!
            for usable_val, expect_refusal in ((31, True), (40, False)):
                caller = SimpleNamespace(
                    accounting=SimpleNamespace(
                        version="caller-requests-v2",
                        configuration=SimpleNamespace(
                            limit=512, commit="a" * 40, digest="d" * 64
                        ),
                        owner=SimpleNamespace(generation=1),
                        action={
                            "intent": {
                                "action": "command",
                                "owner": {"generation": 1},
                                "head_sha": HEAD,
                            }
                        },
                        blocks=(),
                    ),
                    usable=usable_val,
                    _effect_record=rec,
                )
                current_action = SimpleNamespace(
                    run=SimpleNamespace(
                        caller=caller, ledger=ledger, identity=IDENTITY, head_sha=HEAD
                    )
                )
                preview: dict[str, object] = {}
                res = guard_current_command_representability(
                    current_action=current_action,
                    ledger=ledger,
                    identity=IDENTITY,
                    head_sha=HEAD,
                    command=cmd,
                    writes_enabled=True,
                    finding_inventory=finding_inv,
                    capacity_preview=preview,
                )
                if expect_refusal:
                    self.assertIsNotNone(res)
                    assert res is not None
                    self.assertFalse(res.applied)
                    self.assertIn("request_budget_exhausted_before_effect", res.summary)
                else:
                    self.assertIsNone(res)
                    pricing = preview["details"]["pricing"]  # type: ignore[index]
                    self.assertGreater(pricing["receipt_publication_requests"], 8)
                    self.assertGreater(pricing["business_mutation_requests"], 10)

            # `require_action_settlement_representable` also scales receipt pricing when multi-page!
            tight_caller = SimpleNamespace(
                accounting=SimpleNamespace(
                    version="caller-requests-v2",
                    configuration=SimpleNamespace(
                        limit=512, commit="a" * 40, digest="d" * 64
                    ),
                    owner=SimpleNamespace(generation=1),
                    action={
                        "intent": {
                            "action": "command",
                            "owner": {"generation": 1},
                            "head_sha": HEAD,
                        }
                    },
                    blocks=(),
                ),
                usable=5,  # > 3 (single-page 2+1), < 11 (multi-page 10+1)!
                _effect_record=rec,
            )
            fake_run = SimpleNamespace(
                caller=tight_caller,
                ledger=ledger,
                identity=IDENTITY,
                head_sha=HEAD,
            )
            with self.assertRaisesRegex(
                Exception, "request_budget_exhausted_before_effect"
            ):
                require_action_settlement_representable(
                    fake_run,
                    outcome={"kind": "applied", "reason": None},
                    result_document={"applied": True, "summary": "ok"},
                    extra_work_requests=2,
                    action_name="accept-risk",
                )


class ReassessmentSameSecondKeyParityTests(unittest.TestCase):
    """Task 4: `reassessment-v2` and `reassessment-reply-v2` preserve same-second keys and replay-authority guards."""

    def _run_reassessment_for_body(
        self,
        *,
        source_body: str,
        with_bundle: bool,
        recorded_replay_context: str | None = None,
        publish_log: list[dict] | None = None,
    ) -> tuple[str, list[dict]]:
        captured_keys: list[str] = []
        publish_calls = publish_log if publish_log is not None else []
        decisions = (
            HumanAssessmentDecision(
                fingerprint="f" * 64,
                decision="dismissed",
                rationale="Checked",
                human_evidence="Explained",
                diff_evidence="Verified",
            ),
        )
        returned_reply: HumanAssessmentReply
        if recorded_replay_context is not None:
            returned_reply = _RecordedHumanAssessmentReply(
                body="Reassessed",
                decisions=decisions,
                replay_context=recorded_replay_context,
            )
        else:
            returned_reply = HumanAssessmentReply(
                body="Reassessed",
                decisions=decisions,
            )

        def fake_through_operation(_self, **kwargs):
            captured_keys.append(str(kwargs["delivery_id"]))
            self.assertEqual(kwargs["delivery_id"], kwargs["grant"])
            return returned_reply

        finding = HumanReviewFinding(
            fingerprint="f" * 64,
            path="src/app.py",
            body="Confirm invariant",
        )
        inventory = PendingHumanReview(
            base_sha=BASE,
            findings=(finding,),
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
            head_sha=HEAD,
            context=ConversationContext(
                pull_request_number=7,
                base_sha=BASE,
                head_sha=HEAD,
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
            opener=lambda *_args, **_kwargs: json_response({}, status=404),
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
                expected_head_sha=HEAD,
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
        self.assertEqual(len(captured_keys), 1)
        return captured_keys[0], publish_calls

    def test_reassessment_and_reassessment_reply_keys_preserve_same_second_key_and_reject_mismatched_replay_context(
        self,
    ) -> None:
        body_a = "@reviewsensei fixed in commit A"
        body_b = "@reviewsensei fixed in commit B"

        for with_bundle, prefix in (
            (True, "reassessment-v2"),
            (False, "reassessment-reply-v2"),
        ):
            with self.subTest(prefix=prefix):
                key_a, calls_a = self._run_reassessment_for_body(
                    source_body=body_a, with_bundle=with_bundle
                )
                key_b, calls_b = self._run_reassessment_for_body(
                    source_body=body_b, with_bundle=with_bundle
                )
                expected_key = f"{prefix}:issue:41:2026-10-26T00:00:00Z"
                self.assertEqual(key_a, expected_key)
                self.assertEqual(key_b, expected_key)
                self.assertEqual(len(calls_a), 1)
                self.assertEqual(len(calls_b), 1)

                rejected_calls: list[dict] = []
                with self.assertRaisesRegex(
                    ReviewInputError, "human assessment replay authority changed"
                ):
                    self._run_reassessment_for_body(
                        source_body=body_b,
                        with_bundle=with_bundle,
                        recorded_replay_context="c" * 64,
                        publish_log=rejected_calls,
                    )
                self.assertEqual(rejected_calls, [])


if __name__ == "__main__":
    unittest.main()
