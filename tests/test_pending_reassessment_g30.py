"""Zero-decision pending reassessment attribution and recovery tests (G30).

Covers:
- Strict distinction between unfinished zero-decision human reassessment
  (`finalizer_entered=False`, `finalizer_failed=False`, durable pending receipt
  confirmed) and failed approval/check/thread finalization after durably
  published reassessment decisions (`finalizer_entered=True`, `finalizer_failed=True`);
- Fresh-process replay reconciling stored operation journal + durable pending
  receipts with zero duplicate comments and zero extra inference calls;
- Duplicate/conflicting pending-receipt detection failing closed;
- Delayed follow-up sequence (`S1` zero-decision -> `S2` partial -> `S3` final)
  preserving receipt attribution and reaching native `APPROVED` completion;
- Edited source, changed head, and changed base negative controls;
- Bounded consecutive zero-decision no-progress exhaustion;
- End-to-end bounded operator recovery of an unclosed `action.phase == "pending"`
  session ledger via `build_pending_reassessment_recovery_descriptor` +
  `ControlledRestart` (`restart-run`).
"""

from __future__ import annotations

import copy
import hashlib
import json
import socket
import tempfile
import unittest
from dataclasses import replace
from typing import Any
from unittest.mock import patch
from urllib.parse import urlsplit

import review_sensei_github.cli as cli
import review_sensei_github.controlled_restart as restart
import review_sensei_github.review_run as review_run
from fake_github_http import json_response
from review_sensei_github.api_run_host import review_startup_plan
from review_sensei_github.api_run_plan import owner_allocations
from review_sensei_github.conversation import ReplyResult, reply_marker
from review_sensei_github.errors import GitHubPublicationTransientError
from review_sensei_github.http import GitHubHttp
from review_sensei_github.operation_comments import (
    GitHubIssueCommentOperationPort,
    ResultPartStore,
)
from review_sensei_github.operation_host import GitHubCommentOperationStore
from review_sensei_github.pending_reassessment_recovery import (
    NO_PROGRESS_ZERO_DECISION_LIMIT,
    OFFLINE_CONTINUATION_KINDS,
    PENDING_REASSESSMENT_CLASSIFIER_ID,
    PENDING_REASSESSMENT_PACKET_SCHEMA,
    PENDING_WORK_REASONS,
    REVIEWSENSEI_408_CASE_ID,
    REVIEWSENSEI_408_PULL_REQUEST,
    REVIEWSENSEI_408_REPOSITORY,
    OfflineContinuationQualification,
    PendingReassessmentAttemptPacket,
    PendingReassessmentAttribution,
    PendingReassessmentRecoveryDescriptor,
    PendingReassessmentWindow,
    PendingReceiptObservation,
    ReassessmentProgressSummary,
    build_pending_reassessment_attempt_packet,
    build_pending_reassessment_recovery_descriptor,
    evaluate_reassessment_progress,
    inspect_durable_pending_receipts,
    qualify_offline_pending_continuation,
    qualify_pending_reassessment,
)
from review_sensei_github.publication import ReviewApprovalFinalizer
from review_sensei_github.recovery_route_advisor import classify_recovery_state
from review_sensei_github.session_ledger import parse_session_comment

from review_sensei.bounded_evidence import EvidenceReadBudget
from review_sensei.errors import ReviewInputError
from review_sensei.session import SessionIdentity, SessionRecord
from tests.test_human_assessment import (
    APP,
    BASE,
    DIFF,
    HEAD,
    HUMAN,
    Broker,
    Provider,
    State,
    prior,
    response_for,
)

REPO_IDENTITY = SessionIdentity(
    repository="owner/repo", pull_request=7, repository_id=123
)


def _unresolved_response(eligibility: Any) -> dict[str, Any]:
    finding = eligibility.human_review.pending[0]
    return {
        "body": "Current evidence does not yet prove remote calls are unreachable.",
        "assessments": [
            {
                "fingerprint": finding.fingerprint,
                "decision": "unresolved",
                "rationale": "Only one caller branch was explained; verify the fallback.",
                "human_evidence": "This path is intentionally local-only",
                "diff_evidence": "reject_remote_requests(data)",
            }
        ],
    }


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _attach_finding_index(state: State) -> State:
    from review_sensei_github.publication import finding_index_marker

    marker = finding_index_marker(
        repository_id=1,
        pull_request=1,
        head_sha=HEAD,
        base_sha=BASE,
        result_digest=state.eligibility.result_digest,
        entries=(),
    )
    state.reviews[0]["body"] = state.reviews[0]["body"] + "\n" + marker
    return state


def _app_reply(
    state: State,
    provider: Provider,
    *,
    source_id: int = 10,
    updated: str = "updated",
) -> tuple[ReplyResult, Broker]:
    from review_sensei_github import GitHubApplication, GitHubWriteOptions
    from review_sensei_github.conversation import ConversationPublisher

    broker = Broker()
    application = GitHubApplication(
        broker=broker,
        reviewer=None,
        learner=None,
        replier=ConversationPublisher(http=state.http),
        http=state.http,
    )
    result = application.generate_and_publish_reply(
        options=GitHubWriteOptions(
            github_writes=True,
            mention_replies=True,
        ),
        oidc_token="synthetic-oidc",
        read_token="read",
        repository="owner/repo",
        pull_request=1,
        source_comment_id=source_id,
        source_updated_at=updated,
        expected_head_sha=HEAD,
        reply_provider=provider,
        model="fixture-model",
        app_slug=APP,
        root_comment_id=source_id,
        source_kind="issue",
    )
    assert isinstance(result, ReplyResult)
    return result, broker


class PendingReassessmentG30Tests(unittest.TestCase):
    """Offline synthetic verification of G30 zero-decision pending reassessment."""

    def test_positive_continuation_requires_known_original_allowance(self):
        from review_sensei.api_work import ApiRunAccounting

        original = prior(extra_human=True)
        packet = build_pending_reassessment_attempt_packet(
            identity=SessionIdentity(
                repository="owner/repo", pull_request=77, repository_id=987654
            ),
            original_eligibility=original,
            latest_eligibility=original,
            bound_source_comment_id=40810,
            bound_source_updated_at="2026-10-15T14:22:00Z",
            bound_source_body_sha256=_sha256(HUMAN),
            bound_source_kind="issue",
            bound_base_sha=BASE,
            bound_head_sha=HEAD,
            current_base_sha=BASE,
            current_head_sha=HEAD,
            pending_receipt=PendingReceiptObservation(
                source_comment_id=40810,
                source_updated_digest=_sha256("2026-10-15T14:22:00Z"),
                pull_request=77,
                head_sha=HEAD,
                report_digest="c" * 64,
                expected_pages=2,
                part_comment_ids=((1, 7709),),
                complete=False,
            ),
            durable_operation_journal_present=True,
            required_continuation_requests=1,
        )
        qualification = packet.offline_continuation
        self.assertEqual(
            packet.attribution.state_code, "pending_receipt_delivery_incomplete"
        )
        self.assertFalse(qualification.qualifies_zero_inference_continuation)
        self.assertFalse(qualification.zero_new_funding_verified)
        self.assertIsNone(qualification.remaining_caller_allowance)
        self.assertIn(
            "original_caller_allowance_unknown", qualification.disqualifying_reasons
        )
        zero = qualify_offline_pending_continuation(
            packet.attribution,
            durable_operation_journal_present=True,
            required_continuation_requests=0,
        )
        self.assertTrue(zero.qualifies_zero_inference_continuation)
        self.assertIsNone(zero.remaining_caller_allowance)
        with self.assertRaises(ReviewInputError):
            replace(zero, required_continuation_requests=1)
        record = self._make_pending_reply_record(source_body_sha256=_sha256(HUMAN))
        remaining = ApiRunAccounting.from_document(record.api_run_accounting).remaining
        for required, qualified in ((remaining, True), (remaining + 1, False)):
            with self.subTest(required=required):
                bounded = qualify_offline_pending_continuation(
                    packet.attribution,
                    record=record,
                    durable_operation_journal_present=True,
                    required_continuation_requests=required,
                )
                self.assertEqual(
                    bounded.qualifies_zero_inference_continuation, qualified
                )
                self.assertEqual(bounded.zero_new_funding_verified, qualified)
                self.assertEqual(bounded.remaining_caller_allowance, remaining)

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._socket_patch = patch.object(
            socket,
            "create_connection",
            side_effect=AssertionError(
                "real network sockets are forbidden in G30 tests"
            ),
        )
        cls._socket_patch.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._socket_patch.stop()
        super().tearDownClass()

    def test_zero_decision_reassessment_vs_failed_finalizer_distinction_and_attribution(
        self,
    ) -> None:
        # Part A: Zero-decision reassessment (`decisions` all `"unresolved"`):
        # Publishes durable pending receipt, NEVER acquires review/check tokens,
        # NEVER enters finalizer!
        two_item_elig = prior(extra_human=True)
        state_zero = State(two_item_elig)
        zero_provider = Provider(_unresolved_response(two_item_elig))
        zero_outcome, zero_broker = state_zero.application_reply(zero_provider)

        self.assertEqual(zero_outcome.status, "replied")
        self.assertIsNotNone(zero_outcome.comment_id)
        self.assertIsNone(zero_outcome.finalization_completion)
        self.assertEqual(zero_broker.capabilities, ["issue_reply"])
        self.assertEqual(state_zero.events(), [])
        self.assertEqual(state_zero.reply_count, 1)

        finalizer_zero = ReviewApprovalFinalizer(
            http=state_zero.http, evidence_budget=EvidenceReadBudget()
        )
        latest_zero = finalizer_zero.load_eligibility(
            token="read",
            repository="owner/repo",
            pull_request=1,
            head_sha=HEAD,
            app_slug=APP,
        )
        self.assertEqual(latest_zero, two_item_elig)

        receipt_obs = inspect_durable_pending_receipts(
            http=state_zero.http,
            token="read",
            repository="owner/repo",
            pull_request=1,
            source_kind="issue",
            source_comment_id=10,
            source_updated_at="updated",
            head_sha=HEAD,
            app_slug=APP,
        )
        assert receipt_obs is not None
        self.assertTrue(receipt_obs.complete)
        self.assertFalse(receipt_obs.duplicate_conflict)
        self.assertTrue(receipt_obs.content_digests_valid)
        self.assertEqual(receipt_obs.terminal_comment_id, zero_outcome.comment_id)

        attr_zero = qualify_pending_reassessment(
            original_eligibility=two_item_elig,
            latest_eligibility=latest_zero,
            bound_source_comment_id=10,
            bound_source_updated_at="updated",
            bound_source_body_sha256=_sha256(HUMAN),
            bound_source_kind="issue",
            bound_base_sha=BASE,
            bound_head_sha=HEAD,
            current_base_sha=BASE,
            current_head_sha=HEAD,
            source_actor="alice",
            reply_result=zero_outcome,
            pending_receipt=receipt_obs,
        )
        self.assertEqual(attr_zero.state_code, "unfinished_zero_decision_assessment")
        self.assertFalse(attr_zero.terminal)
        self.assertTrue(attr_zero.assessment_unfinished)
        self.assertTrue(attr_zero.zero_decisions_recorded)
        self.assertFalse(attr_zero.finalizer_entered)
        self.assertFalse(attr_zero.finalizer_failed)
        self.assertFalse(attr_zero.authority)
        self.assertEqual(len(attr_zero.pending_finding_ids), 2)
        self.assertEqual(attr_zero.newly_resolved_finding_ids, ())
        self.assertEqual(attr_zero.prior_receipt_digests, (receipt_obs.report_digest,))
        self.assertEqual(attr_zero.primary_route, "delayed_followup_authorized_mention")
        self.assertEqual(attr_zero.to_dict()["authority"], False)

        # Part B: Contrast with failed finalizer AFTER reassessment decisions ARE
        # durably recorded in an eligibility `COMMENT` review:
        state_fin = _attach_finding_index(State())
        fin_provider = Provider(response_for(state_fin.eligibility))
        with patch.object(
            ReviewApprovalFinalizer,
            "finalize",
            side_effect=GitHubPublicationTransientError(
                "transient check/approval loss"
            ),
        ):
            interrupted, fin_broker = _app_reply(state_fin, fin_provider)

        self.assertEqual(interrupted.status, "replied")
        self.assertEqual(interrupted.approval_status, "approval_withheld")
        self.assertIsNone(interrupted.finalization_completion)
        self.assertIn("review_publish", fin_broker.capabilities)
        self.assertEqual(state_fin.events(), ["COMMENT"])

        latest_fin = ReviewApprovalFinalizer(
            http=state_fin.http, evidence_budget=EvidenceReadBudget()
        ).load_eligibility(
            token="read",
            repository="owner/repo",
            pull_request=1,
            head_sha=HEAD,
            app_slug=APP,
        )
        assert latest_fin is not None
        self.assertNotEqual(latest_fin, state_fin.eligibility)
        self.assertEqual(latest_fin.human_review.pending, ())

        attr_failed_fin = qualify_pending_reassessment(
            original_eligibility=state_fin.eligibility,
            latest_eligibility=latest_fin,
            bound_source_comment_id=10,
            bound_source_updated_at="updated",
            bound_source_body_sha256=_sha256(HUMAN),
            bound_source_kind="issue",
            bound_base_sha=BASE,
            bound_head_sha=HEAD,
            current_base_sha=BASE,
            current_head_sha=HEAD,
            source_actor="alice",
            reply_result=interrupted,
        )
        self.assertEqual(
            attr_failed_fin.state_code, "failed_finalizer_after_reassessment"
        )
        self.assertFalse(attr_failed_fin.terminal)
        self.assertFalse(attr_failed_fin.assessment_unfinished)
        self.assertFalse(attr_failed_fin.zero_decisions_recorded)
        self.assertTrue(attr_failed_fin.finalizer_entered)
        self.assertTrue(attr_failed_fin.finalizer_failed)
        self.assertEqual(attr_failed_fin.primary_route, "resume_exact_head_finalizer")

        # Fresh-process finalizer resume succeeds with ZERO new inference calls:
        no_call_provider = Provider({})
        resumed, _ = _app_reply(state_fin, no_call_provider)
        self.assertEqual(no_call_provider.calls, [])
        self.assertEqual(resumed.approval_status, "approved")
        self.assertIsNotNone(resumed.finalization_completion)
        self.assertEqual(state_fin.events(), ["COMMENT", "APPROVE"])

        attr_settled = qualify_pending_reassessment(
            original_eligibility=state_fin.eligibility,
            latest_eligibility=latest_fin,
            bound_source_comment_id=10,
            bound_source_updated_at="updated",
            bound_source_body_sha256=_sha256(HUMAN),
            bound_source_kind="issue",
            bound_base_sha=BASE,
            bound_head_sha=HEAD,
            current_base_sha=BASE,
            current_head_sha=HEAD,
            source_actor="alice",
            reply_result=resumed,
        )
        self.assertEqual(attr_settled.state_code, "reassessment_completed_settled")
        self.assertTrue(attr_settled.terminal)
        self.assertEqual(attr_settled.primary_route, "ordinary_rollover")

    def test_fresh_process_replay_duplicate_suppression_and_delayed_followup_completion(
        self,
    ) -> None:
        from review_sensei_github import GitHubApplication, GitHubWriteOptions
        from review_sensei_github.conversation import ConversationPublisher

        # 2-finding human inventory:
        orig_elig = prior(extra_human=True)
        state = _attach_finding_index(State(orig_elig))
        state.diff = (
            "path=src/app.py\n"
            + DIFF
            + "\npath=src/other.py\n@@ -1 +1 @@\n-send_other(x)\n+reject_other(x)"
        )
        # Make `State.open` aware of bot user_id=999 and multi-source IDs 10/11/12:
        orig_open = state.open
        sources_by_id: dict[int, dict[str, Any]] = {10: state.source}

        def _tag_users(payload: Any) -> Any:
            if isinstance(payload, list):
                for item in payload:
                    _tag_users(item)
                return payload
            if isinstance(payload, dict) and isinstance(payload.get("user"), dict):
                if payload["user"].get("type") == "Bot":
                    payload["user"].setdefault("id", 999)
                else:
                    payload["user"].setdefault("id", 42)
            return payload

        def open_with_bot_id(request: Any, timeout: float = 30) -> Any:
            path = urlsplit(request.full_url).path
            sources_by_id[int(state.source["id"])] = state.source
            if request.method == "GET" and "/issues/comments/" in path:
                cid = int(path.rsplit("/", 1)[-1])
                if cid in sources_by_id:
                    return json_response(_tag_users(copy.deepcopy(sources_by_id[cid])))
            if request.method == "PATCH" and "/issues/comments/" in path:
                cid = int(path.rsplit("/", 1)[-1])
                req_body = json.loads(request.data) if request.data else {}
                for item in state.replies:
                    if item["id"] == cid:
                        item["body"] = req_body["body"]
                        item["updated_at"] = "patched"
                        return json_response(_tag_users(copy.deepcopy(item)), 200)
                return json_response({}, 404)
            if request.method == "POST" and path.endswith("/reactions"):
                return json_response({"id": 99}, 201)
            if request.method == "GET" and path.endswith("/issues/1/comments"):
                return json_response(
                    _tag_users(copy.deepcopy([*sources_by_id.values(), *state.replies]))
                )
            resp = orig_open(request, timeout)
            raw = resp.body
            data = json.loads(raw) if raw else {}
            return json_response(_tag_users(data), resp.status)

        state.http = GitHubHttp(
            api_url="https://api.github.test", opener=open_with_bot_id
        )

        def run_fresh_process_reply(
            prov: Provider, *, source_id: int, updated: str
        ) -> ReplyResult:
            port = GitHubIssueCommentOperationPort(
                state.http,
                token="synthetic-issue_reply",
                repository="owner/repo",
                pull_request=1,
                app_user_id=999,
                strict_association=True,
            )
            op_store = GitHubCommentOperationStore(port, app_user_id=999)
            op_parts = ResultPartStore(port, app_user_id=999)
            app = GitHubApplication(
                broker=Broker(),
                reviewer=None,
                learner=None,
                replier=ConversationPublisher(http=state.http),
                http=state.http,
            )
            res = app.generate_and_publish_reply(
                options=GitHubWriteOptions(
                    github_writes=True,
                    mention_replies=True,
                    operation_entry="enabled",
                ),
                oidc_token="synthetic-oidc",
                read_token="read",
                repository="owner/repo",
                pull_request=1,
                source_comment_id=source_id,
                source_updated_at=updated,
                expected_head_sha=HEAD,
                reply_provider=prov,
                model="test-model",
                app_slug=APP,
                root_comment_id=source_id,
                source_kind="issue",
                operation_entry="enabled",
                repository_id=123,
                app_id=999,
                run_id="7001",
                run_attempt="1",
                operation_store=op_store,
                operation_parts=op_parts,
                evidence_budget=EvidenceReadBudget(),
            )
            assert isinstance(res, ReplyResult)
            return res

        # Step 1 (S1): Zero-decision reassessment in Process 1 writes operation
        # receipt + 1 pending-report comment:
        state.source["updated_at"] = "t1"
        p1 = Provider(_unresolved_response(orig_elig))
        r1 = run_fresh_process_reply(p1, source_id=10, updated="t1")
        self.assertEqual(r1.status, "replied")
        self.assertEqual(len(p1.calls), 1)
        obs1 = inspect_durable_pending_receipts(
            http=state.http,
            token="read",
            repository="owner/repo",
            pull_request=1,
            source_kind="issue",
            source_comment_id=10,
            source_updated_at="t1",
            head_sha=HEAD,
            app_slug=APP,
        )
        assert obs1 is not None and obs1.complete
        reply_comments_after_p1 = len(state.replies)

        # Step 1b: Fresh Process 2 replays the exact same S1 (`source_id=10, updated="t1"`):
        # ZERO new provider calls and ZERO duplicate pending-receipt comments!
        state.source["updated_at"] = "t1"
        p2_cold = Provider({})
        r1_replay = run_fresh_process_reply(p2_cold, source_id=10, updated="t1")
        self.assertEqual(p2_cold.calls, [])
        self.assertEqual(r1_replay.status, "already_replied")
        self.assertEqual(r1_replay.comment_id, r1.comment_id)
        self.assertEqual(len(state.replies), reply_comments_after_p1)

        # Step 1c: Duplicate/conflicting receipt injection fails closed!
        state.replies.append(
            {
                **copy.deepcopy(state.replies[-1]),
                "id": 999,
            }
        )
        dup_obs = inspect_durable_pending_receipts(
            http=state.http,
            token="read",
            repository="owner/repo",
            pull_request=1,
            source_kind="issue",
            source_comment_id=10,
            source_updated_at="t1",
            head_sha=HEAD,
            app_slug=APP,
        )
        assert dup_obs is not None
        self.assertTrue(dup_obs.duplicate_conflict)
        self.assertFalse(dup_obs.complete)
        dup_attr = qualify_pending_reassessment(
            original_eligibility=orig_elig,
            latest_eligibility=orig_elig,
            bound_source_comment_id=10,
            bound_source_updated_at="t1",
            bound_source_body_sha256=_sha256(state.source["body"]),
            bound_source_kind="issue",
            bound_base_sha=BASE,
            bound_head_sha=HEAD,
            current_base_sha=BASE,
            current_head_sha=HEAD,
            pending_receipt=dup_obs,
        )
        self.assertEqual(
            dup_attr.state_code, "pending_reassessment_duplicate_receipt_conflict"
        )
        state.replies.pop()  # remove injected duplicate

        # Step 2 (Delayed follow-up S2): Clears finding 1 (`src/app.py`), leaves
        # finding 2 (`src/other.py`) pending -> partial progress!
        f_app = next(
            f for f in orig_elig.human_review.pending if f.path == "src/app.py"
        )
        f_other = next(
            f for f in orig_elig.human_review.pending if f.path == "src/other.py"
        )
        s2_body = (
            "@sensei `src/app.py` calls `reject_remote_requests(data)` unconditionally."
        )
        state.source = {
            **state.source,
            "id": 11,
            "body": s2_body,
            "updated_at": "t2",
        }
        p_s2 = Provider(
            {
                "body": "Cleared src/app.py; src/other.py still awaits evidence.",
                "assessments": [
                    {
                        "fingerprint": f_app.fingerprint,
                        "decision": "dismissed",
                        "rationale": "Current diff calls reject_remote_requests(data).",
                        "human_evidence": "calls `reject_remote_requests(data)` unconditionally",
                        "diff_evidence": "reject_remote_requests(data)",
                    }
                ],
            }
        )
        # Ensure `State.open` serves diff for both files during HumanAssessmentPublisher:
        with patch.object(
            ConversationPublisher,
            "load_human_assessment_diff",
            return_value=type(
                "_Diff",
                (),
                {"diff_context": state.diff, "diagnostic": None},
            )(),
        ):
            r_s2, _ = _app_reply(state, p_s2, source_id=11, updated="t2")
        self.assertEqual(r_s2.status, "replied")
        self.assertIsNotNone(r_s2.finalization_completion)
        self.assertEqual(state.events(), ["COMMENT"])

        obs2 = inspect_durable_pending_receipts(
            http=state.http,
            token="read",
            repository="owner/repo",
            pull_request=1,
            source_kind="issue",
            source_comment_id=11,
            source_updated_at="t2",
            head_sha=HEAD,
            app_slug=APP,
        )
        assert obs2 is not None and obs2.complete
        elig_after_s2 = ReviewApprovalFinalizer(
            http=state.http, evidence_budget=EvidenceReadBudget()
        ).load_eligibility(
            token="read",
            repository="owner/repo",
            pull_request=1,
            head_sha=HEAD,
            app_slug=APP,
        )
        assert elig_after_s2 is not None
        attr_s2 = qualify_pending_reassessment(
            original_eligibility=orig_elig,
            latest_eligibility=elig_after_s2,
            bound_source_comment_id=11,
            bound_source_updated_at="t2",
            bound_source_body_sha256=_sha256(s2_body),
            bound_source_kind="issue",
            bound_base_sha=BASE,
            bound_head_sha=HEAD,
            current_base_sha=BASE,
            current_head_sha=HEAD,
            reply_result=r_s2,
            pending_receipt=obs2,
            prior_receipt_digests=(obs1.report_digest,),
        )
        self.assertEqual(attr_s2.state_code, "unfinished_partial_decision_assessment")
        self.assertTrue(attr_s2.terminal)
        self.assertEqual(attr_s2.pending_finding_ids, (f_other.fingerprint,))
        self.assertEqual(attr_s2.newly_resolved_finding_ids, (f_app.fingerprint,))
        self.assertEqual(
            attr_s2.prior_receipt_digests,
            (obs1.report_digest, obs2.report_digest),
        )

        # Step 3 (Delayed follow-up S3): Clears remaining finding 2 (`src/other.py`)
        # and reaches native `APPROVED` review!
        s3_body = "@sensei `src/other.py` now calls `reject_other(x)` on line 1."
        state.source = {
            **state.source,
            "id": 12,
            "body": s3_body,
            "updated_at": "t3",
        }
        p_s3 = Provider(
            {
                "body": "Both human-adjudicated paths are now verified.",
                "assessments": [
                    {
                        "fingerprint": f_other.fingerprint,
                        "decision": "dismissed",
                        "rationale": "Current diff replaces send_other with reject_other.",
                        "human_evidence": "calls `reject_other(x)` on line 1",
                        "diff_evidence": "+reject_other(x)",
                    }
                ],
            }
        )
        with patch.object(
            ConversationPublisher,
            "load_human_assessment_diff",
            return_value=type(
                "_Diff",
                (),
                {"diff_context": state.diff, "diagnostic": None},
            )(),
        ):
            r_s3, _ = _app_reply(state, p_s3, source_id=12, updated="t3")
        self.assertEqual(r_s3.approval_status, "approved")
        self.assertEqual(state.events(), ["COMMENT", "COMMENT", "APPROVE"])

        elig_after_s3 = ReviewApprovalFinalizer(
            http=state.http, evidence_budget=EvidenceReadBudget()
        ).load_eligibility(
            token="read",
            repository="owner/repo",
            pull_request=1,
            head_sha=HEAD,
            app_slug=APP,
        )
        attr_s3 = qualify_pending_reassessment(
            original_eligibility=elig_after_s2,
            latest_eligibility=elig_after_s3,
            bound_source_comment_id=12,
            bound_source_updated_at="t3",
            bound_source_body_sha256=_sha256(s3_body),
            bound_source_kind="issue",
            bound_base_sha=BASE,
            bound_head_sha=HEAD,
            current_base_sha=BASE,
            current_head_sha=HEAD,
            reply_result=r_s3,
            prior_receipt_digests=attr_s2.prior_receipt_digests,
        )
        self.assertEqual(attr_s3.state_code, "reassessment_completed_settled")
        self.assertTrue(attr_s3.terminal)
        self.assertEqual(attr_s3.pending_finding_ids, ())
        self.assertEqual(
            attr_s3.prior_receipt_digests,
            (obs1.report_digest, obs2.report_digest),
        )

    def test_changed_source_changed_head_base_and_no_progress_exhaustion_controls(
        self,
    ) -> None:
        orig_elig = prior(extra_human=True)
        pending_ids = tuple(f.fingerprint for f in orig_elig.human_review.pending)

        # 1. Edited source comment refuses stale replay (`pending_reassessment_source_changed`)
        attr_src = qualify_pending_reassessment(
            original_eligibility=orig_elig,
            latest_eligibility=orig_elig,
            bound_source_comment_id=10,
            bound_source_updated_at="t1",
            bound_source_body_sha256=_sha256(HUMAN),
            bound_source_kind="issue",
            bound_base_sha=BASE,
            bound_head_sha=HEAD,
            current_base_sha=BASE,
            current_head_sha=HEAD,
            live_source_updated_at="t1-edited",
            live_source_body_sha256=_sha256(HUMAN + " edited"),
        )
        self.assertEqual(attr_src.state_code, "pending_reassessment_source_changed")
        self.assertFalse(attr_src.terminal)
        self.assertEqual(attr_src.primary_route, "delayed_followup_authorized_mention")

        # 2. Changed head & changed base refuse continuing old pending scope
        for new_base, new_head in ((BASE, "e" * 40), ("c" * 40, HEAD)):
            attr_drift = qualify_pending_reassessment(
                original_eligibility=orig_elig,
                latest_eligibility=orig_elig,
                bound_source_comment_id=10,
                bound_source_updated_at="t1",
                bound_source_body_sha256=_sha256(HUMAN),
                bound_source_kind="issue",
                bound_base_sha=BASE,
                bound_head_sha=HEAD,
                current_base_sha=new_base,
                current_head_sha=new_head,
            )
            self.assertEqual(
                attr_drift.state_code, "pending_reassessment_head_or_base_changed"
            )
            self.assertFalse(attr_drift.terminal)
            self.assertEqual(attr_drift.primary_route, "controlled_serialized_restart")

        # 3. Multi-window progress & no-progress exhaustion accounting:
        w1 = PendingReassessmentWindow(
            source_comment_id=10,
            source_updated_at="t1",
            source_body_sha256=_sha256("reply 1"),
            head_sha=HEAD,
            base_sha=BASE,
            pending_before=pending_ids,
            pending_after=pending_ids,
            report_digest="1" * 64,
            receipt_comment_id=30,
        )
        # Exact duplicate replay of w1 does NOT increment consecutive_zero_decision_attempts!
        w1_dup = PendingReassessmentWindow(
            source_comment_id=10,
            source_updated_at="t1",
            source_body_sha256=_sha256("reply 1"),
            head_sha=HEAD,
            base_sha=BASE,
            pending_before=pending_ids,
            pending_after=pending_ids,
            report_digest="1" * 64,
            receipt_comment_id=30,
        )
        summary_after_dup = evaluate_reassessment_progress(
            [w1, w1_dup], initial_pending_ids=pending_ids, max_zero_decision_attempts=2
        )
        self.assertEqual(summary_after_dup.distinct_window_count, 1)
        self.assertEqual(summary_after_dup.duplicate_replay_count, 1)
        self.assertEqual(summary_after_dup.consecutive_zero_decision_attempts, 1)
        self.assertFalse(summary_after_dup.no_progress_exhausted)

        # Second distinct zero-decision window w2 hits no-progress exhaustion (2/2):
        w2 = PendingReassessmentWindow(
            source_comment_id=11,
            source_updated_at="t2",
            source_body_sha256=_sha256("reply 2 without evidence"),
            head_sha=HEAD,
            base_sha=BASE,
            pending_before=pending_ids,
            pending_after=pending_ids,
            report_digest="2" * 64,
            receipt_comment_id=31,
        )
        exhausted = evaluate_reassessment_progress(
            [w1, w1_dup, w2],
            initial_pending_ids=pending_ids,
            max_zero_decision_attempts=2,
        )
        self.assertTrue(exhausted.no_progress_exhausted)
        self.assertEqual(exhausted.consecutive_zero_decision_attempts, 2)
        self.assertEqual(exhausted.attributed_receipt_digests, ("1" * 64, "2" * 64))
        self.assertEqual(exhausted.attributed_source_ids, (10, 11))

        obs_w2 = PendingReceiptObservation(
            source_comment_id=11,
            source_updated_digest=_sha256("t2"),
            pull_request=1,
            head_sha=HEAD,
            report_digest="2" * 64,
            expected_pages=1,
            part_comment_ids=((1, 31),),
            complete=True,
        )
        attr_exhausted = qualify_pending_reassessment(
            original_eligibility=orig_elig,
            latest_eligibility=orig_elig,
            bound_source_comment_id=11,
            bound_source_updated_at="t2",
            bound_source_body_sha256=_sha256("reply 2 without evidence"),
            bound_source_kind="issue",
            bound_base_sha=BASE,
            bound_head_sha=HEAD,
            current_base_sha=BASE,
            current_head_sha=HEAD,
            pending_receipt=obs_w2,
            prior_receipt_digests=exhausted.attributed_receipt_digests,
            consecutive_zero_decision_attempts=exhausted.consecutive_zero_decision_attempts,
            max_zero_decision_attempts=exhausted.max_zero_decision_attempts,
        )
        self.assertEqual(
            attr_exhausted.state_code, "pending_reassessment_no_progress_exhausted"
        )
        self.assertEqual(attr_exhausted.primary_route, "targeted_human_disposition")

        # Productive window w3 resets consecutive_zero_decision_attempts to 0:
        w3_productive = PendingReassessmentWindow(
            source_comment_id=12,
            source_updated_at="t3",
            source_body_sha256=_sha256("reply 3 resolving first item"),
            head_sha=HEAD,
            base_sha=BASE,
            pending_before=pending_ids,
            pending_after=(pending_ids[1],),
            report_digest="3" * 64,
            receipt_comment_id=32,
        )
        reset_summary = evaluate_reassessment_progress(
            [w1, w3_productive],
            initial_pending_ids=pending_ids,
            max_zero_decision_attempts=2,
        )
        self.assertEqual(reset_summary.consecutive_zero_decision_attempts, 0)
        self.assertFalse(reset_summary.no_progress_exhausted)
        self.assertEqual(reset_summary.cumulative_resolved_ids, (pending_ids[0],))
        self.assertEqual(reset_summary.remaining_pending_ids, (pending_ids[1],))

    def test_wedged_pending_reassessment_ledger_recovers_to_completion_via_descriptor(
        self,
    ) -> None:
        from test_action_reply_entry import ReplyEntryFixture
        from test_review_run import EntryFixture
        from test_review_transaction import HostedAnalysisCliTests

        # 1. Publish an initial `needs_human` review, then run an `execute_reply`
        #    action whose reassessment clears ZERO findings -> durable pending
        #    receipt is posted and `action.phase == "pending"` remains on ledger!
        with tempfile.TemporaryDirectory() as wedged_dir:
            base_f = EntryFixture(wedged_dir)
            self.assertEqual(base_f.invoke(human=True), 0, base_f.error)

            rf_reply = ReplyEntryFixture.__new__(ReplyEntryFixture)
            rf_reply.__dict__.update(base_f.__dict__)
            rf_reply.next_run()
            rf_reply.live_head = rf_reply.requested_head = base_f.live_head
            rf_reply.prepare_reply()
            rf_reply.source["body"] = (
                "@reviewsensei Can you clarify why this path needs human confirmation?"
            )
            rf_reply.reply_document = {
                "body": "Clarified the concern; human evidence is still needed.",
                "assessments": [],
            }
            self.assertEqual(rf_reply.invoke_reply(), 1)
            self.assertIn(
                "reply assessment or required finalization remains pending",
                rf_reply.error,
            )

            wedged_rec = parse_session_comment(
                rf_reply.comment["body"], identity=REPO_IDENTITY
            )
            assert wedged_rec is not None
            assert wedged_rec.api_run_accounting is not None
            self.assertEqual(
                wedged_rec.api_run_accounting["action"]["phase"], "pending"
            )
            self.assertTrue(
                wedged_rec.api_run_accounting["action"]["execution_started"]
            )

            # Cross-check G03 `classify_recovery_state` agrees on `pending_action_started_unfinished`:
            g03_same_head = classify_recovery_state(
                wedged_rec,
                current_base=rf_reply.requested_base,
                current_head=rf_reply.requested_head,
            )
            self.assertEqual(
                g03_same_head.state_code, "pending_action_started_unfinished"
            )
            self.assertEqual(
                g03_same_head.primary_route, "pending_reassessment_continuation"
            )

            # Build G30 attribution on the wedged session record:
            sample_elig = prior()
            obs_stub = PendingReceiptObservation(
                source_comment_id=rf_reply.source["id"],
                source_updated_digest=_sha256(rf_reply.source["updated_at"]),
                pull_request=7,
                head_sha=rf_reply.requested_head,
                report_digest="a" * 64,
                expected_pages=1,
                part_comment_ids=((1, 301),),
                complete=True,
            )
            wedged_attr = qualify_pending_reassessment(
                original_eligibility=sample_elig,
                latest_eligibility=sample_elig,
                bound_source_comment_id=rf_reply.source["id"],
                bound_source_updated_at=rf_reply.source["updated_at"],
                bound_source_body_sha256=_sha256(rf_reply.source["body"]),
                bound_source_kind="issue",
                bound_base_sha=rf_reply.requested_base,
                bound_head_sha=rf_reply.requested_head,
                current_base_sha=rf_reply.requested_base,
                current_head_sha=rf_reply.requested_head,
                pending_receipt=obs_stub,
                record=wedged_rec,
            )
            self.assertEqual(
                wedged_attr.state_code, "unfinished_zero_decision_assessment"
            )
            self.assertIsNotNone(wedged_attr.operation_key)
            self.assertEqual(wedged_attr.predecessor_run_id, rf_reply.run_id)

            old_comment = copy.deepcopy(rf_reply.comment)
            old_comment["id"] = 77
            old_raw = old_comment["body"].encode("utf-8")
            old_ref = {
                "id": old_comment["id"],
                "bytes": len(old_raw),
                "sha256": hashlib.sha256(old_raw).hexdigest(),
            }
            old_run_id = rf_reply.run_id
            initial_run_id = base_f.run_id

            # Before predecessor owner ceases, descriptor builder refuses!
            with self.assertRaisesRegex(ReviewInputError, "before owner ceases"):
                build_pending_reassessment_recovery_descriptor(
                    identity=REPO_IDENTITY,
                    record=wedged_rec,
                    attribution=wedged_attr,
                    owner_ceased=False,
                    old_comments=[old_ref],
                    designated_run_id="6000",
                    base_sha=rf_reply.requested_base,
                    head_sha=rf_reply.requested_head,
                    policy_commit=rf_reply.policy,
                    workflow_sha="a" * 40,
                    issuance_disabled_at=1000,
                    retire_after=4600,
                )

            # Execute bounded ControlledRestart recovery on a new designated run:
            with tempfile.TemporaryDirectory() as restart_dir:
                rf = EntryFixture(restart_dir)
                rf.run_id = 6000
                rf.event_name = "workflow_dispatch"
                descriptor = build_pending_reassessment_recovery_descriptor(
                    identity=REPO_IDENTITY,
                    record=wedged_rec,
                    attribution=wedged_attr,
                    owner_ceased=True,
                    old_comments=[old_ref],
                    designated_run_id=str(rf.run_id),
                    base_sha=rf.requested_base,
                    head_sha=rf.requested_head,
                    policy_commit=rf.policy,
                    workflow_sha="a" * 40,
                    issuance_disabled_at=1000,
                    retire_after=4600,
                    extra_old_runs=[initial_run_id],
                )
                self.assertIsInstance(descriptor, PendingReassessmentRecoveryDescriptor)
                self.assertFalse(descriptor.authority)
                self.assertEqual(
                    descriptor.classifier_id, PENDING_REASSESSMENT_CLASSIFIER_ID
                )
                self.assertEqual(
                    descriptor.pending_finding_ids, wedged_attr.pending_finding_ids
                )
                self.assertEqual(descriptor.prior_receipt_digests, ("a" * 64,))
                self.assertEqual(
                    descriptor.registry_document()["schema_version"],
                    "controlled-restart-registry-v1",
                )
                self.assertEqual(descriptor.to_dict()["authority"], False)
                self.assertEqual(
                    descriptor.to_dict()["request_id"], descriptor.request_id
                )

                row_data = descriptor.controlled_restart_registration
                original_external = rf.external
                original_main = cli.main

                def external(request: Any, timeout: float) -> Any:
                    path = urlsplit(request.full_url).path
                    for past_id in (old_run_id, initial_run_id):
                        if path.endswith(f"/actions/runs/{past_id}"):
                            rf.trace.append((rf.attempt, request.method, path))
                            return json_response(
                                {
                                    "id": past_id,
                                    "repository": {
                                        "id": 123,
                                        "full_name": "owner/repo",
                                    },
                                    "status": "completed",
                                    "run_attempt": 1,
                                }
                            )
                        if path.endswith(f"/actions/runs/{past_id}/attempts/1/jobs"):
                            rf.trace.append((rf.attempt, request.method, path))
                            return json_response(
                                {
                                    "total_count": 1,
                                    "jobs": [
                                        {
                                            "id": 10 + past_id,
                                            "run_id": past_id,
                                            "run_attempt": 1,
                                            "status": "completed",
                                        }
                                    ],
                                }
                            )
                    if path.endswith(f"/issues/comments/{old_comment['id']}"):
                        rf.trace.append((rf.attempt, request.method, path))
                        return json_response(old_comment)
                    response = original_external(request, timeout)
                    if path.endswith("/issues/7/comments") and request.method == "GET":
                        return json_response([old_comment, *json.loads(response.body)])
                    return response

                rf.external = external

                def restart_main(argv: list[str]) -> int:
                    argv = [
                        "restart-run",
                        "--restart-id",
                        descriptor.request_id,
                        *argv[1:],
                    ]
                    if "--diff" in argv:
                        idx = argv.index("--diff")
                        del argv[idx : idx + 2]
                    return original_main(argv)

                def prepare_diff(**kwargs: Any) -> None:
                    kwargs["output"].write_text(
                        HostedAnalysisCliTests.ANALYSIS_DIFF,
                        encoding="utf-8",
                        newline="\n",
                    )

                with (
                    patch.object(restart, "registrations", return_value=[row_data]),
                    patch.object(cli, "main", side_effect=restart_main),
                    patch.object(review_run, "prepare_diff", side_effect=prepare_diff),
                ):
                    self.assertEqual(rf.invoke(), 0, rf.error)
                self.assertIsNotNone(rf.selected.caller.accounting.terminal_transaction)
                archived = json.loads((rf.root / "abandoned-history.json").read_bytes())
                self.assertEqual(archived["comments"][0]["body"], old_comment["body"])

    def test_adversarial_invariants_and_incomplete_receipt_branches(self) -> None:
        orig_elig = prior()
        fid = orig_elig.human_review.pending[0].fingerprint

        # 1. Incomplete pending receipt (`part=1/2` without `2/2`) -> `pending_receipt_delivery_incomplete`
        base_m = (
            reply_marker(
                source_comment_id=10,
                source_updated_digest=_sha256("t1"),
                pull_request=1,
                head_sha=HEAD,
            )[:-4]
            + f" report={'9' * 64} part="
        )
        page1_text = "## Still pending\nPage 1"
        page1_sha = _sha256(page1_text)
        comments_state: list[Any] = [
            "skip-non-dict",
            {
                "id": 50,
                "user": {"login": APP, "type": "Bot"},
                "body": f"{page1_text}\n\n{base_m}1/2 content={page1_sha} -->",
            },
        ]

        def inline_opener(request: Any, timeout: float | None = None) -> Any:
            return json_response(comments_state)

        http = GitHubHttp(api_url="https://api.github.test", opener=inline_opener)
        partial_obs = inspect_durable_pending_receipts(
            http=http,
            token="tok",
            repository="owner/repo",
            pull_request=1,
            source_kind="inline",
            source_comment_id=10,
            source_updated_at="t1",
            head_sha=HEAD,
            app_slug=APP,
            expected_report_digest="9" * 64,
        )
        assert partial_obs is not None
        self.assertFalse(partial_obs.complete)
        self.assertIsNone(partial_obs.terminal_comment_id)

        attr_incomplete = qualify_pending_reassessment(
            original_eligibility=orig_elig,
            latest_eligibility=orig_elig,
            bound_source_comment_id=10,
            bound_source_updated_at="t1",
            bound_source_body_sha256=_sha256(HUMAN),
            bound_source_kind="inline",
            bound_base_sha=BASE,
            bound_head_sha=HEAD,
            current_base_sha=BASE,
            current_head_sha=HEAD,
            pending_receipt=partial_obs,
        )
        self.assertEqual(
            attr_incomplete.state_code, "pending_receipt_delivery_incomplete"
        )
        self.assertEqual(
            attr_incomplete.primary_route, "replay_same_source_pending_receipt"
        )

        # Corrupt content digest, mismatched expected digest, malformed tail, non-Bot author:
        comments_state = [
            {
                "id": 51,
                "user": {"login": APP, "type": "User"},  # non-Bot spoof with app_slug!
                "body": f"{page1_text}\n\n{base_m}1/1 content={page1_sha} -->",
            }
        ]
        spoof_obs = inspect_durable_pending_receipts(
            http=http,
            token="tok",
            repository="owner/repo",
            pull_request=1,
            source_kind="inline",
            source_comment_id=10,
            source_updated_at="t1",
            head_sha=HEAD,
            app_slug=APP,
        )
        assert spoof_obs is not None
        self.assertTrue(spoof_obs.duplicate_conflict)

        comments_state = [
            {
                "id": 52,
                "user": {"login": APP, "type": "Bot"},
                "body": f"tampered\n\n{base_m}1/1 content={page1_sha} -->",
            }
        ]
        tampered_obs = inspect_durable_pending_receipts(
            http=http,
            token="tok",
            repository="owner/repo",
            pull_request=1,
            source_kind="inline",
            source_comment_id=10,
            source_updated_at="t1",
            head_sha=HEAD,
            app_slug=APP,
        )
        assert tampered_obs is not None
        self.assertFalse(tampered_obs.content_digests_valid)
        self.assertFalse(tampered_obs.complete)

        # Empty comments -> None:
        comments_state = []
        self.assertIsNone(
            inspect_durable_pending_receipts(
                http=http,
                token="tok",
                repository="owner/repo",
                pull_request=1,
                source_kind="inline",
                source_comment_id=10,
                source_updated_at="t1",
                head_sha=HEAD,
                app_slug=APP,
            )
        )
        with self.assertRaises(ReviewInputError):
            inspect_durable_pending_receipts(
                http=http,
                token="tok",
                repository="owner/repo",
                pull_request=1,
                source_kind="bogus",
                source_comment_id=10,
                source_updated_at="t1",
                head_sha=HEAD,
                app_slug=APP,
            )

        # 2. Dataclass & progress evaluator invariant rejections:
        with self.assertRaises(ReviewInputError):
            PendingReceiptObservation(
                source_comment_id=0,
                source_updated_digest="1" * 64,
                pull_request=1,
                head_sha=HEAD,
                report_digest="2" * 64,
                expected_pages=1,
                part_comment_ids=((1, 10),),
                complete=True,
            )
        with self.assertRaises(ReviewInputError):
            PendingReceiptObservation(
                source_comment_id=10,
                source_updated_digest="1" * 64,
                pull_request=1,
                head_sha=HEAD,
                report_digest="2" * 64,
                expected_pages=2,
                part_comment_ids=((1, 10),),
                complete=True,
            )
        with self.assertRaises(ReviewInputError):
            PendingReassessmentWindow(
                source_comment_id=10,
                source_updated_at="t1",
                source_body_sha256="1" * 64,
                head_sha=HEAD,
                base_sha=BASE,
                pending_before=(),
                pending_after=(fid,),  # invented finding ID -> rejected!
            )
        with self.assertRaises(ReviewInputError):
            ReassessmentProgressSummary(
                distinct_window_count=1,
                duplicate_replay_count=0,
                consecutive_zero_decision_attempts=0,
                max_zero_decision_attempts=2,
                no_progress_exhausted=False,
                remaining_pending_ids=(),
                cumulative_resolved_ids=(),
                attributed_receipt_digests=(),
                attributed_source_ids=(),
                authority=True,
            )
        w_base = PendingReassessmentWindow(
            source_comment_id=10,
            source_updated_at="t1",
            source_body_sha256="1" * 64,
            head_sha=HEAD,
            base_sha=BASE,
            pending_before=(fid,),
            pending_after=(fid,),
            report_digest="a" * 64,
        )
        w_drift = replace(w_base, source_comment_id=11, head_sha="f" * 40)
        with self.assertRaisesRegex(ReviewInputError, "head or base drift"):
            evaluate_reassessment_progress(
                [w_base, w_drift], initial_pending_ids=(fid,)
            )
        w_conflict_dup = replace(w_base, pending_after=())
        with self.assertRaisesRegex(ReviewInputError, "conflicts with recorded"):
            evaluate_reassessment_progress(
                [w_base, w_conflict_dup], initial_pending_ids=(fid,)
            )
        w_diverge = PendingReassessmentWindow(
            source_comment_id=11,
            source_updated_at="t2",
            source_body_sha256="2" * 64,
            head_sha=HEAD,
            base_sha=BASE,
            pending_before=(),
            pending_after=(),
        )
        with self.assertRaisesRegex(ReviewInputError, "diverges"):
            evaluate_reassessment_progress(
                [w_base, w_diverge], initial_pending_ids=(fid,)
            )

        for bad_attr in (
            {"authority": True},
            {"classifier_id": "bad"},
            {"state_code": "bad"},
            {"primary_route": "bad"},
            {"finalizer_entered": True},
            {
                "state_code": "failed_finalizer_after_reassessment",
                "finalizer_entered": False,
                "finalizer_failed": False,
                "primary_route": "resume_exact_head_finalizer",
            },
        ):
            valid_attr_kwargs: dict[str, Any] = {
                "classifier_id": PENDING_REASSESSMENT_CLASSIFIER_ID,
                "state_code": "unfinished_zero_decision_assessment",
                "terminal": False,
                "assessment_unfinished": True,
                "zero_decisions_recorded": True,
                "finalizer_entered": False,
                "finalizer_failed": False,
                "source_comment_id": 10,
                "source_updated_at": "t1",
                "source_body_sha256": "1" * 64,
                "source_kind": "issue",
                "source_actor": "alice",
                "bound_base_sha": BASE,
                "bound_head_sha": HEAD,
                "current_base_sha": BASE,
                "current_head_sha": HEAD,
                "pending_finding_ids": (fid,),
                "resolved_finding_ids": (),
                "newly_resolved_finding_ids": (),
                "pending_receipt": None,
                "prior_receipt_digests": (),
                "consecutive_zero_decision_attempts": 1,
                "max_zero_decision_attempts": 2,
                "evidence_diagnostic": None,
                "predecessor_run_id": None,
                "operation_key": None,
                "primary_route": "delayed_followup_authorized_mention",
                "fallback_routes": (),
                "operator_action": "ok",
            }
            valid_attr_kwargs.update(bad_attr)
            with self.assertRaises(ReviewInputError):
                PendingReassessmentAttribution(**valid_attr_kwargs)

        with self.assertRaises(ReviewInputError):
            PendingReassessmentRecoveryDescriptor(
                classifier_id=PENDING_REASSESSMENT_CLASSIFIER_ID,
                attribution_state="unfinished_zero_decision_assessment",
                predecessor_run_id=1,
                predecessor_root_sha256="1" * 64,
                retained_nonstartup_caller_liability=1,
                retained_total_caller_liability=5,
                pending_finding_ids=(),
                prior_receipt_digests=(),
                controlled_restart_registration={"request_id": "a" * 64},
                operator_instructions="ok",
                authority=True,
            )

        # Malformed receipt marker shapes (invalid hex digest, expected_report mismatch,
        # malformed tail, part_idx > total_pages, conflicting report digests) fail closed:
        raw_base = (
            reply_marker(
                source_comment_id=10,
                source_updated_digest=_sha256("t1"),
                pull_request=1,
                head_sha=HEAD,
            )[:-4]
            + " report="
        )
        for bad_body, exp_rep in (
            (f"p\n\n{raw_base}short", None),
            (f"p\n\n{raw_base}{'Z' * 64} part=1/1 content={'1' * 64} -->", None),
            (
                f"p\n\n{raw_base}{'8' * 64} part=1/1 content={'1' * 64} -->",
                "9" * 64,
            ),
            (f"p\n\n{raw_base}{'9' * 64} part=bad -->", None),
            (f"p\n\n{raw_base}{'9' * 64} part=3/2 content={_sha256('p')} -->", None),
        ):
            comments_state.clear()
            comments_state.append(
                {
                    "id": 60,
                    "user": {"login": APP, "type": "Bot"},
                    "body": bad_body,
                }
            )
            mal_obs = inspect_durable_pending_receipts(
                http=http,
                token="tok",
                repository="owner/repo",
                pull_request=1,
                source_kind="inline",
                source_comment_id=10,
                source_updated_at="t1",
                head_sha=HEAD,
                app_slug=APP,
                expected_report_digest=exp_rep,
            )
            assert mal_obs is not None
            self.assertFalse(mal_obs.complete)
            self.assertTrue(mal_obs.duplicate_conflict)

        # Two conflicting report digests on same source/head:
        comments_state.clear()
        comments_state.extend(
            [
                {
                    "id": 61,
                    "user": {"login": APP, "type": "Bot"},
                    "body": f"p\n\n{raw_base}{'8' * 64} part=1/2 content={_sha256('p')} -->",
                },
                {
                    "id": 62,
                    "user": {"login": APP, "type": "Bot"},
                    "body": f"p\n\n{raw_base}{'9' * 64} part=2/2 content={_sha256('p')} -->",
                },
            ]
        )
        multi_rep_obs = inspect_durable_pending_receipts(
            http=http,
            token="tok",
            repository="owner/repo",
            pull_request=1,
            source_kind="inline",
            source_comment_id=10,
            source_updated_at="t1",
            head_sha=HEAD,
            app_slug=APP,
        )
        assert multi_rep_obs is not None
        self.assertTrue(multi_rep_obs.duplicate_conflict)
        self.assertFalse(multi_rep_obs.complete)

        # Validation guards on qualify_pending_reassessment & descriptor builder:
        with self.assertRaises(ReviewInputError):
            qualify_pending_reassessment(
                original_eligibility=orig_elig,
                latest_eligibility=orig_elig,
                bound_source_comment_id=10,
                bound_source_updated_at="t1",
                bound_source_body_sha256="1" * 64,
                bound_source_kind="issue",
                bound_base_sha=BASE,
                bound_head_sha=HEAD,
                current_base_sha=BASE,
                current_head_sha=HEAD,
                max_zero_decision_attempts=0,
            )
        bare_record = SessionRecord._construct(
            repository="owner/repo",
            pull_request=7,
            repository_id=123,
            completed_initial_reviews=0,
            completed_verification_rounds=0,
            failed_attempts=0,
            generation=0,
            reservation_id=None,
            reserved_slot=None,
            last_committed_reservation_id=None,
            created_at="2026-11-10T12:00:00Z",
            updated_at="2026-11-10T12:00:00Z",
            expires_at="2026-12-10T12:00:00Z",
        )
        with self.assertRaises(ReviewInputError):
            build_pending_reassessment_recovery_descriptor(
                identity="bad",  # type: ignore[arg-type]
                record=bare_record,
                attribution=PendingReassessmentAttribution(
                    classifier_id=PENDING_REASSESSMENT_CLASSIFIER_ID,
                    state_code="unfinished_zero_decision_assessment",
                    terminal=False,
                    assessment_unfinished=True,
                    zero_decisions_recorded=True,
                    finalizer_entered=False,
                    finalizer_failed=False,
                    source_comment_id=10,
                    source_updated_at="t1",
                    source_body_sha256="1" * 64,
                    source_kind="issue",
                    source_actor="alice",
                    bound_base_sha=BASE,
                    bound_head_sha=HEAD,
                    current_base_sha=BASE,
                    current_head_sha=HEAD,
                    pending_finding_ids=(fid,),
                    resolved_finding_ids=(),
                    newly_resolved_finding_ids=(),
                    pending_receipt=None,
                    prior_receipt_digests=(),
                    consecutive_zero_decision_attempts=1,
                    max_zero_decision_attempts=2,
                    evidence_diagnostic=None,
                    predecessor_run_id=None,
                    operation_key=None,
                    primary_route="delayed_followup_authorized_mention",
                    fallback_routes=(),
                    operator_action="ok",
                ),
                owner_ceased=True,
                old_comments=[],
                designated_run_id="6001",
                base_sha=BASE,
                head_sha=HEAD,
                policy_commit=BASE,
                workflow_sha=BASE,
                issuance_disabled_at=100,
                retire_after=200,
            )
        with self.assertRaises(ReviewInputError):
            build_pending_reassessment_recovery_descriptor(
                identity=REPO_IDENTITY,
                record=bare_record,
                attribution=PendingReassessmentAttribution(
                    classifier_id=PENDING_REASSESSMENT_CLASSIFIER_ID,
                    state_code="unfinished_zero_decision_assessment",
                    terminal=False,
                    assessment_unfinished=True,
                    zero_decisions_recorded=True,
                    finalizer_entered=False,
                    finalizer_failed=False,
                    source_comment_id=10,
                    source_updated_at="t1",
                    source_body_sha256="1" * 64,
                    source_kind="issue",
                    source_actor="alice",
                    bound_base_sha=BASE,
                    bound_head_sha=HEAD,
                    current_base_sha=BASE,
                    current_head_sha=HEAD,
                    pending_finding_ids=(fid,),
                    resolved_finding_ids=(),
                    newly_resolved_finding_ids=(),
                    pending_receipt=None,
                    prior_receipt_digests=(),
                    consecutive_zero_decision_attempts=1,
                    max_zero_decision_attempts=2,
                    evidence_diagnostic=None,
                    predecessor_run_id=None,
                    operation_key=None,
                    primary_route="delayed_followup_authorized_mention",
                    fallback_routes=(),
                    operator_action="ok",
                ),
                owner_ceased=True,
                old_comments=[],
                designated_run_id="6001",
                base_sha=BASE,
                head_sha=HEAD,
                policy_commit=BASE,
                workflow_sha=BASE,
                issuance_disabled_at=100,
                retire_after=200,
            )

        # RS-540BF2: `override`/`accept-risk` pending-set reduction WITHOUT adding
        # to `resolved` is still recognized as `failed_finalizer_after_reassessment`
        # when finalization has not settled!
        obs_first = PendingReceiptObservation(
            source_comment_id=10,
            source_updated_digest=_sha256("t1"),
            pull_request=1,
            head_sha=HEAD,
            report_digest="d" * 64,
            expected_pages=1,
            part_comment_ids=((1, 40),),
            complete=True,
        )
        two_item = prior(extra_human=True)
        assert two_item.human_review is not None
        overridden_inv = replace(
            two_item.human_review,
            findings=two_item.human_review.findings[1:],
            resolved=two_item.human_review.resolved,
        )
        overridden_elig = replace(two_item, human_review=overridden_inv)
        attr_override_fin = qualify_pending_reassessment(
            original_eligibility=two_item,
            latest_eligibility=overridden_elig,
            bound_source_comment_id=10,
            bound_source_updated_at="t1",
            bound_source_body_sha256="1" * 64,
            bound_source_kind="issue",
            bound_base_sha=BASE,
            bound_head_sha=HEAD,
            current_base_sha=BASE,
            current_head_sha=HEAD,
            pending_receipt=obs_first,
        )
        self.assertEqual(
            attr_override_fin.state_code, "failed_finalizer_after_reassessment"
        )
        self.assertTrue(attr_override_fin.finalizer_entered)
        self.assertTrue(attr_override_fin.finalizer_failed)
        self.assertEqual(attr_override_fin.primary_route, "resume_exact_head_finalizer")

        # RS-9BFA77 & RS-04CE57: monotonic durable-receipt derivation when
        # `consecutive_zero_decision_attempts` is omitted, plus `>= 1` & source_kind guards!
        self.assertEqual(NO_PROGRESS_ZERO_DECISION_LIMIT, 2)
        obs_second = PendingReceiptObservation(
            source_comment_id=11,
            source_updated_digest=_sha256("t2"),
            pull_request=1,
            head_sha=HEAD,
            report_digest="e" * 64,
            expected_pages=1,
            part_comment_ids=((1, 41),),
            complete=True,
        )
        auto_exhausted = qualify_pending_reassessment(
            original_eligibility=orig_elig,
            latest_eligibility=orig_elig,
            bound_source_comment_id=11,
            bound_source_updated_at="t2",
            bound_source_body_sha256="2" * 64,
            bound_source_kind="issue",
            bound_base_sha=BASE,
            bound_head_sha=HEAD,
            current_base_sha=BASE,
            current_head_sha=HEAD,
            pending_receipt=obs_second,
            prior_receipt_digests=(obs_first.report_digest,),
        )
        self.assertEqual(auto_exhausted.consecutive_zero_decision_attempts, 2)
        self.assertEqual(
            auto_exhausted.state_code, "pending_reassessment_no_progress_exhausted"
        )
        with self.assertRaisesRegex(
            ReviewInputError, "consecutive_zero_decision_attempts >= 1"
        ):
            qualify_pending_reassessment(
                original_eligibility=orig_elig,
                latest_eligibility=orig_elig,
                bound_source_comment_id=10,
                bound_source_updated_at="t1",
                bound_source_body_sha256="1" * 64,
                bound_source_kind="issue",
                bound_base_sha=BASE,
                bound_head_sha=HEAD,
                current_base_sha=BASE,
                current_head_sha=HEAD,
                pending_receipt=obs_first,
                consecutive_zero_decision_attempts=0,
            )
        with self.assertRaisesRegex(ReviewInputError, "invalid bound_source_kind"):
            qualify_pending_reassessment(
                original_eligibility=orig_elig,
                latest_eligibility=orig_elig,
                bound_source_comment_id=10,
                bound_source_updated_at="t1",
                bound_source_body_sha256="1" * 64,
                bound_source_kind="attacker_kind",
                bound_base_sha=BASE,
                bound_head_sha=HEAD,
                current_base_sha=BASE,
                current_head_sha=HEAD,
            )

    def _make_pending_reply_record(
        self,
        *,
        repository: str = REVIEWSENSEI_408_REPOSITORY,
        pull_request: int = REVIEWSENSEI_408_PULL_REQUEST,
        repository_id: int = 987654,
        run_id: int = 408001,
        limit: int = 512,
        source_id: int = 40810,
        source_updated_at: str = "2026-10-15T14:22:00Z",
        source_body_sha256: str = "1" * 64,
        head_sha: str = HEAD,
        generation: int = 3,
    ) -> SessionRecord:
        from review_sensei.api_work import (
            ApiExecutionOwner,
            ApiRunAccounting,
            ApiRunIdentity,
            FrozenApiConfiguration,
            initial_accounting,
        )

        config = FrozenApiConfiguration(commit=BASE, digest="2" * 64, limit=limit)
        owner = ApiExecutionOwner(1, "hosted", 0)
        plan = review_startup_plan("hosted")
        allocation = owner_allocations(limit, plan.liability)
        accounting = (
            initial_accounting(
                ApiRunIdentity(repository_id, run_id),
                config,
                owner,
                plan,
                prior_nonstartup_liability_upper_bound=0,
            )
            .reserve("hosted-review", allocation.first_work)
            .reserve("hosted-terminal", allocation.first_terminal)
        )
        doc = accounting.to_document()
        doc["action"] = {
            "intent": {
                "purpose": "reply",
                "source": {
                    "kind": "issue_comment",
                    "id": source_id,
                    "updated_at": source_updated_at,
                    "body_sha256": source_body_sha256,
                },
                "run_id": run_id,
                "owner": {
                    "run_attempt": 1,
                    "job": "hosted",
                    "generation": 0,
                },
                "head_sha": head_sha,
                "configuration_digest": config.digest,
                "operation_key": "a" * 64,
                "source_root": "b" * 64,
            },
            "phase": "pending",
            "execution_started": True,
            "result": None,
            "outcome": None,
        }
        validated = ApiRunAccounting.from_document(doc)
        return SessionRecord._construct(
            repository=repository,
            pull_request=pull_request,
            repository_id=repository_id,
            completed_initial_reviews=1,
            completed_verification_rounds=0,
            failed_attempts=0,
            generation=generation,
            reservation_id=None,
            reserved_slot=None,
            last_committed_reservation_id="5" * 64,
            created_at="2026-10-15T14:00:00Z",
            updated_at="2026-10-15T14:22:30Z",
            expires_at="2026-11-15T14:00:00Z",
            api_run_accounting=validated.to_document(),
        )

    def test_reviewsensei_408_offline_zero_decision_attempt_packet_and_finalizer_contrast(
        self,
    ) -> None:
        id_408 = SessionIdentity(
            repository=REVIEWSENSEI_408_REPOSITORY,
            pull_request=REVIEWSENSEI_408_PULL_REQUEST,
            repository_id=987654,
        )
        orig_elig = prior(extra_human=True)
        assert orig_elig.human_review is not None
        source_sha = _sha256(HUMAN)
        rec_408 = self._make_pending_reply_record(source_body_sha256=source_sha)
        receipt_408 = PendingReceiptObservation(
            source_comment_id=40810,
            source_updated_digest=_sha256("2026-10-15T14:22:00Z"),
            pull_request=REVIEWSENSEI_408_PULL_REQUEST,
            head_sha=HEAD,
            report_digest="c" * 64,
            expected_pages=1,
            part_comment_ids=((1, 40899),),
            complete=True,
        )

        pkt_408 = build_pending_reassessment_attempt_packet(
            identity=id_408,
            original_eligibility=orig_elig,
            latest_eligibility=orig_elig,
            bound_source_comment_id=40810,
            bound_source_updated_at="2026-10-15T14:22:00Z",
            bound_source_body_sha256=source_sha,
            bound_source_kind="issue",
            bound_base_sha=BASE,
            bound_head_sha=HEAD,
            current_base_sha=BASE,
            current_head_sha=HEAD,
            pr_merged=True,
            record=rec_408,
            source_actor="maintainer-user",
            pending_receipt=receipt_408,
        )

        # A lookalike/foreign repository must not inherit this historical incident label.
        for foreign_repository in (
            "other/review-sensei",
            "malsabbagh/review-sensei-copy",
        ):
            with self.subTest(foreign_repository=foreign_repository):
                foreign_packet = build_pending_reassessment_attempt_packet(
                    identity=SessionIdentity(
                        repository=foreign_repository,
                        pull_request=id_408.pull_request,
                        repository_id=id_408.repository_id,
                    ),
                    original_eligibility=orig_elig,
                    latest_eligibility=orig_elig,
                    bound_source_comment_id=40810,
                    bound_source_updated_at="2026-10-15T14:22:00Z",
                    bound_source_body_sha256=source_sha,
                    bound_source_kind="issue",
                    bound_base_sha=BASE,
                    bound_head_sha=HEAD,
                    current_base_sha=BASE,
                    current_head_sha=HEAD,
                    pr_merged=True,
                    record=SessionRecord._construct(
                        **{
                            **{
                                key: value
                                for key, value in vars(rec_408).items()
                                if key != "record_sha256" and not key.startswith("_")
                            },
                            "repository": foreign_repository,
                        }
                    ),
                    source_actor="maintainer-user",
                    pending_receipt=receipt_408,
                )
                self.assertNotEqual(foreign_packet.case_id, REVIEWSENSEI_408_CASE_ID)
                self.assertTrue(
                    foreign_packet.case_id.startswith(foreign_repository + "#")
                )
                self.assertFalse(foreign_packet.authority)
        self.assertIsInstance(pkt_408, PendingReassessmentAttemptPacket)
        self.assertEqual(pkt_408.case_id, REVIEWSENSEI_408_CASE_ID)
        self.assertFalse(pkt_408.authority)
        self.assertTrue(pkt_408.offline_only)
        self.assertFalse(pkt_408.zero_decision_is_completion_witness)
        self.assertTrue(pkt_408.pr_merged)
        self.assertEqual(
            pkt_408.pending_reason, "zero_decision_human_explanation_incomplete"
        )
        self.assertIn(pkt_408.pending_reason, PENDING_WORK_REASONS)
        self.assertTrue(pkt_408.assessment_unfinished)
        self.assertTrue(pkt_408.zero_decisions_recorded)
        self.assertFalse(pkt_408.finalizer_entered)
        self.assertFalse(pkt_408.finalizer_failed)
        self.assertTrue(pkt_408.retained_result_immutable)
        self.assertEqual(pkt_408.original_result_digest, orig_elig.result_digest)
        self.assertEqual(pkt_408.latest_result_digest, orig_elig.result_digest)
        self.assertEqual(pkt_408.accepted_result_sha256, orig_elig.result_digest)
        self.assertEqual(pkt_408.predecessor_run_id, 408001)
        self.assertEqual(pkt_408.operation_key, "a" * 64)
        self.assertFalse(
            pkt_408.offline_continuation.qualifies_zero_inference_continuation
        )
        self.assertFalse(pkt_408.offline_continuation.completion_witness_established)
        self.assertIn(
            "merged_pr_live_rerun_forbidden",
            pkt_408.offline_continuation.disqualifying_reasons,
        )
        self.assertIn(
            "zero_decisions_already_delivered_pending_receipt",
            pkt_408.offline_continuation.disqualifying_reasons,
        )
        self.assertEqual(
            set(pkt_408.missing_proofs),
            {
                "completed_reassessment_decisions",
                "terminal_action_outcome",
                "missing_close_diagnostics",
                "installed_executor_provenance",
                "historical_live_settlement_receipt",
            },
        )
        serialized_408 = pkt_408.to_dict()
        self.assertEqual(
            serialized_408["schema_version"], PENDING_REASSESSMENT_PACKET_SCHEMA
        )
        self.assertFalse(serialized_408["authority"])
        self.assertTrue(serialized_408["offline_only"])
        self.assertFalse(serialized_408["zero_decision_is_completion_witness"])
        self.assertEqual(
            serialized_408["pending_work_identity"]["pull_request"],
            REVIEWSENSEI_408_PULL_REQUEST,
        )

        # Contrast 1: Unmerged incomplete pending receipt delivery with durable journal
        # qualifies zero-funding / zero-inference replay without completion witness.
        id_unmerged = SessionIdentity(
            repository="owner/repo", pull_request=77, repository_id=123
        )
        rec_unmerged = self._make_pending_reply_record(
            repository="owner/repo",
            pull_request=77,
            repository_id=123,
            run_id=7701,
            source_body_sha256=source_sha,
        )
        incomplete_obs = PendingReceiptObservation(
            source_comment_id=40810,
            source_updated_digest=_sha256("2026-10-15T14:22:00Z"),
            pull_request=77,
            head_sha=HEAD,
            report_digest="c" * 64,
            expected_pages=2,
            part_comment_ids=((1, 7709),),
            complete=False,
        )
        pkt_replay = build_pending_reassessment_attempt_packet(
            identity=id_unmerged,
            original_eligibility=orig_elig,
            latest_eligibility=orig_elig,
            bound_source_comment_id=40810,
            bound_source_updated_at="2026-10-15T14:22:00Z",
            bound_source_body_sha256=source_sha,
            bound_source_kind="issue",
            bound_base_sha=BASE,
            bound_head_sha=HEAD,
            current_base_sha=BASE,
            current_head_sha=HEAD,
            pr_merged=False,
            record=rec_unmerged,
            pending_receipt=incomplete_obs,
            durable_operation_journal_present=True,
            required_continuation_requests=2,
            executor_sha="e" * 40,
            installed_core_version="0.8.12",
            installed_hosted_version="0.3.13",
            diagnostics_available=True,
        )
        self.assertEqual(pkt_replay.case_id, "owner/repo#77:gen-3:run-7701")
        self.assertEqual(
            pkt_replay.pending_reason, "pending_receipt_delivery_incomplete"
        )
        self.assertTrue(
            pkt_replay.offline_continuation.qualifies_zero_inference_continuation
        )
        self.assertEqual(
            pkt_replay.offline_continuation.continuation_kind,
            "replay_stored_pending_receipt",
        )
        self.assertIn(
            pkt_replay.offline_continuation.continuation_kind,
            OFFLINE_CONTINUATION_KINDS,
        )
        self.assertFalse(pkt_replay.offline_continuation.completion_witness_established)
        self.assertIn("complete_durable_pending_receipt", pkt_replay.missing_proofs)
        self.assertNotIn("installed_executor_provenance", pkt_replay.missing_proofs)
        self.assertNotIn("missing_close_diagnostics", pkt_replay.missing_proofs)

        # Without durable journal, replay is disqualified cleanly:
        pkt_no_journal = build_pending_reassessment_attempt_packet(
            identity=id_unmerged,
            original_eligibility=orig_elig,
            latest_eligibility=orig_elig,
            bound_source_comment_id=40810,
            bound_source_updated_at="2026-10-15T14:22:00Z",
            bound_source_body_sha256=source_sha,
            bound_source_kind="issue",
            bound_base_sha=BASE,
            bound_head_sha=HEAD,
            current_base_sha=BASE,
            current_head_sha=HEAD,
            pr_merged=False,
            pending_receipt=incomplete_obs,
            durable_operation_journal_present=False,
        )
        self.assertFalse(
            pkt_no_journal.offline_continuation.qualifies_zero_inference_continuation
        )
        self.assertIn(
            "durable_operation_journal_missing",
            pkt_no_journal.offline_continuation.disqualifying_reasons,
        )
        self.assertEqual(pkt_no_journal.case_id, "owner/repo#77:gen-none:run-none")

        # Contrast 2: Failed finalizer after durable decisions qualifies exact-head
        # zero-inference finalizer resumption on an unmerged PR, but NOT on a merged PR!
        resolved_inv = replace(
            orig_elig.human_review,
            resolved=tuple(f.fingerprint for f in orig_elig.human_review.findings),
        )
        resolved_elig = replace(orig_elig, human_review=resolved_inv)
        pkt_fin = build_pending_reassessment_attempt_packet(
            identity=id_unmerged,
            original_eligibility=orig_elig,
            latest_eligibility=resolved_elig,
            bound_source_comment_id=40810,
            bound_source_updated_at="2026-10-15T14:22:00Z",
            bound_source_body_sha256=source_sha,
            bound_source_kind="issue",
            bound_base_sha=BASE,
            bound_head_sha=HEAD,
            current_base_sha=BASE,
            current_head_sha=HEAD,
            pr_merged=False,
            record=rec_unmerged,
            required_continuation_requests=3,
        )
        self.assertEqual(
            pkt_fin.pending_reason, "finalizer_interrupted_after_durable_decisions"
        )
        self.assertTrue(pkt_fin.retained_result_immutable)
        self.assertFalse(pkt_fin.admissible_to_approve)
        self.assertTrue(pkt_fin.finalizer_entered)
        self.assertTrue(pkt_fin.finalizer_failed)
        self.assertFalse(pkt_fin.zero_decisions_recorded)
        self.assertTrue(
            pkt_fin.offline_continuation.qualifies_zero_inference_continuation
        )
        self.assertEqual(
            pkt_fin.offline_continuation.continuation_kind,
            "resume_recorded_decision_finalizer",
        )
        self.assertIn(
            "confirmed_finalizer_check_thread_approval_settlement",
            pkt_fin.missing_proofs,
        )

        pkt_fin_merged = build_pending_reassessment_attempt_packet(
            identity=id_408,
            original_eligibility=orig_elig,
            latest_eligibility=resolved_elig,
            bound_source_comment_id=40810,
            bound_source_updated_at="2026-10-15T14:22:00Z",
            bound_source_body_sha256=source_sha,
            bound_source_kind="issue",
            bound_base_sha=BASE,
            bound_head_sha=HEAD,
            current_base_sha=BASE,
            current_head_sha=HEAD,
            pr_merged=True,
        )
        self.assertFalse(pkt_fin_merged.admissible_to_approve)
        self.assertFalse(
            pkt_fin_merged.offline_continuation.qualifies_zero_inference_continuation
        )
        self.assertIn(
            "merged_pr_live_rerun_forbidden",
            pkt_fin_merged.offline_continuation.disqualifying_reasons,
        )

        # Contrast 3: Settled full vs partial reassessment & remaining state mappings:
        settled_reply = ReplyResult(
            status="replied",
            comment_id=501,
            finalization_completion=object(),  # type: ignore[arg-type]
            resolution_status="resolved",
            approval_status="approved",
        )
        pkt_settled = build_pending_reassessment_attempt_packet(
            identity=id_unmerged,
            original_eligibility=orig_elig,
            latest_eligibility=resolved_elig,
            bound_source_comment_id=40810,
            bound_source_updated_at="2026-10-15T14:22:00Z",
            bound_source_body_sha256=source_sha,
            bound_source_kind="issue",
            bound_base_sha=BASE,
            bound_head_sha=HEAD,
            current_base_sha=BASE,
            current_head_sha=HEAD,
            reply_result=settled_reply,
        )
        self.assertEqual(pkt_settled.pending_reason, "settled_all_human_holds_resolved")
        self.assertTrue(pkt_settled.retained_result_immutable)
        self.assertTrue(pkt_settled.admissible_to_approve)
        self.assertTrue(pkt_settled.to_dict()["admissible_to_approve"])
        self.assertEqual(
            pkt_settled.offline_continuation.continuation_kind, "not_needed_settled"
        )
        self.assertTrue(pkt_settled.offline_continuation.completion_witness_established)
        self.assertEqual(pkt_settled.missing_proofs, ())

        partial_inv = replace(
            orig_elig.human_review,
            resolved=(orig_elig.human_review.findings[1].fingerprint,),
        )
        partial_elig = replace(orig_elig, human_review=partial_inv)
        pkt_partial = build_pending_reassessment_attempt_packet(
            identity=id_unmerged,
            original_eligibility=orig_elig,
            latest_eligibility=partial_elig,
            bound_source_comment_id=40810,
            bound_source_updated_at="2026-10-15T14:22:00Z",
            bound_source_body_sha256=source_sha,
            bound_source_kind="issue",
            bound_base_sha=BASE,
            bound_head_sha=HEAD,
            current_base_sha=BASE,
            current_head_sha=HEAD,
            reply_result=settled_reply,
        )
        self.assertEqual(
            pkt_partial.pending_reason, "partial_reassessment_remaining_human_holds"
        )
        self.assertTrue(pkt_partial.retained_result_immutable)
        self.assertFalse(pkt_partial.admissible_to_approve)
        self.assertFalse(
            pkt_partial.offline_continuation.completion_witness_established
        )
        self.assertEqual(
            pkt_partial.missing_proofs, ("remaining_human_adjudication_decisions",)
        )

        # Contrast 4: Mutated result_digest on the SAME head_sha MUST fail closed
        # (`retained_result_immutable=False`, `admissible_to_approve=False`).
        mutated_same_head_elig = replace(resolved_elig, result_digest="9" * 64)
        pkt_mutated = build_pending_reassessment_attempt_packet(
            identity=id_unmerged,
            original_eligibility=orig_elig,
            latest_eligibility=mutated_same_head_elig,
            bound_source_comment_id=40810,
            bound_source_updated_at="2026-10-15T14:22:00Z",
            bound_source_body_sha256=source_sha,
            bound_source_kind="issue",
            bound_base_sha=BASE,
            bound_head_sha=HEAD,
            current_base_sha=BASE,
            current_head_sha=HEAD,
            pr_merged=False,
            reply_result=settled_reply,
        )
        self.assertFalse(pkt_mutated.retained_result_immutable)
        self.assertFalse(pkt_mutated.admissible_to_approve)
        self.assertFalse(pkt_mutated.to_dict()["admissible_to_approve"])
        self.assertFalse(
            pkt_mutated.to_dict()["accepted_result"]["retained_result_immutable"]
        )
        self.assertFalse(
            pkt_mutated.to_dict()["accepted_result"]["admissible_to_approve"]
        )
        self.assertFalse(
            pkt_mutated.offline_continuation.qualifies_zero_inference_continuation
        )
        self.assertFalse(
            pkt_mutated.offline_continuation.completion_witness_established
        )
        self.assertEqual(
            pkt_mutated.offline_continuation.continuation_kind,
            "not_eligible_requires_new_human_evidence_or_restart",
        )
        self.assertIn(
            "retained_result_digest_mutated",
            pkt_mutated.offline_continuation.disqualifying_reasons,
        )
        self.assertNotIn(
            "action_already_settled",
            pkt_mutated.offline_continuation.disqualifying_reasons,
        )
        self.assertIn("immutable_accepted_result_digest", pkt_mutated.missing_proofs)
        self.assertIn("historical_live_settlement_receipt", pkt_mutated.missing_proofs)
        with self.assertRaisesRegex(ReviewInputError, "fail closed"):
            replace(pkt_mutated, admissible_to_approve=True)

    def test_offline_continuation_and_attempt_packet_validation_guardrails(
        self,
    ) -> None:
        id_77 = SessionIdentity(
            repository="owner/repo", pull_request=77, repository_id=123
        )
        orig_elig = prior()
        source_sha = _sha256(HUMAN)
        rec_low = self._make_pending_reply_record(
            repository="owner/repo",
            pull_request=77,
            repository_id=123,
            run_id=7702,
            source_body_sha256=source_sha,
        )

        # Disqualifiers for new caller/provider funding, repeated inference, and insufficient allowance:
        pkt_disq = build_pending_reassessment_attempt_packet(
            identity=id_77,
            original_eligibility=orig_elig,
            latest_eligibility=orig_elig,
            bound_source_comment_id=40810,
            bound_source_updated_at="2026-10-15T14:22:00Z",
            bound_source_body_sha256=source_sha,
            bound_source_kind="issue",
            bound_base_sha=BASE,
            bound_head_sha=HEAD,
            current_base_sha=BASE,
            current_head_sha=HEAD,
            record=rec_low,
            durable_operation_journal_present=True,
            required_continuation_requests=999,
            additional_caller_allowance=1,
            additional_provider_allowance=1,
            repeated_inference_calls=1,
        )
        self.assertFalse(pkt_disq.offline_continuation.zero_new_funding_verified)
        self.assertFalse(pkt_disq.offline_continuation.zero_repeated_inference_verified)
        self.assertEqual(
            set(pkt_disq.offline_continuation.disqualifying_reasons),
            {
                "new_caller_funding_forbidden",
                "new_provider_funding_forbidden",
                "repeated_model_inference_forbidden",
                "insufficient_remaining_caller_allowance",
            },
        )

        # Cover source-changed, head-drifted, no-progress-exhausted, and duplicate-conflict reasons:
        pkt_src = build_pending_reassessment_attempt_packet(
            identity=id_77,
            original_eligibility=orig_elig,
            latest_eligibility=orig_elig,
            bound_source_comment_id=40810,
            bound_source_updated_at="2026-10-15T14:22:00Z",
            bound_source_body_sha256=source_sha,
            bound_source_kind="issue",
            bound_base_sha=BASE,
            bound_head_sha=HEAD,
            current_base_sha=BASE,
            current_head_sha=HEAD,
            live_source_updated_at="2026-10-15T15:00:00Z",
        )
        self.assertEqual(
            pkt_src.pending_reason, "source_revision_mutated_after_admission"
        )
        self.assertIn(
            "source_comment_revision_changed",
            pkt_src.offline_continuation.disqualifying_reasons,
        )

        pkt_drift = build_pending_reassessment_attempt_packet(
            identity=id_77,
            original_eligibility=orig_elig,
            latest_eligibility=orig_elig,
            bound_source_comment_id=40810,
            bound_source_updated_at="2026-10-15T14:22:00Z",
            bound_source_body_sha256=source_sha,
            bound_source_kind="issue",
            bound_base_sha=BASE,
            bound_head_sha=HEAD,
            current_base_sha=BASE,
            current_head_sha="f" * 40,
        )
        self.assertEqual(
            pkt_drift.pending_reason, "head_or_base_drifted_from_bound_scope"
        )
        self.assertIn(
            "head_or_base_drifted",
            pkt_drift.offline_continuation.disqualifying_reasons,
        )

        dup_obs = PendingReceiptObservation(
            source_comment_id=40810,
            source_updated_digest=_sha256("2026-10-15T14:22:00Z"),
            pull_request=77,
            head_sha=HEAD,
            report_digest="c" * 64,
            expected_pages=1,
            part_comment_ids=((1, 7709),),
            complete=False,
            duplicate_conflict=True,
        )
        pkt_dup = build_pending_reassessment_attempt_packet(
            identity=id_77,
            original_eligibility=orig_elig,
            latest_eligibility=orig_elig,
            bound_source_comment_id=40810,
            bound_source_updated_at="2026-10-15T14:22:00Z",
            bound_source_body_sha256=source_sha,
            bound_source_kind="issue",
            bound_base_sha=BASE,
            bound_head_sha=HEAD,
            current_base_sha=BASE,
            current_head_sha=HEAD,
            pending_receipt=dup_obs,
        )
        self.assertEqual(pkt_dup.pending_reason, "duplicate_or_corrupt_pending_receipt")
        self.assertIn(
            "duplicate_or_corrupt_pending_receipt_conflict",
            pkt_dup.offline_continuation.disqualifying_reasons,
        )

        ok_obs = replace(dup_obs, duplicate_conflict=False, complete=True)
        pkt_exh = build_pending_reassessment_attempt_packet(
            identity=id_77,
            original_eligibility=orig_elig,
            latest_eligibility=orig_elig,
            bound_source_comment_id=40810,
            bound_source_updated_at="2026-10-15T14:22:00Z",
            bound_source_body_sha256=source_sha,
            bound_source_kind="issue",
            bound_base_sha=BASE,
            bound_head_sha=HEAD,
            current_base_sha=BASE,
            current_head_sha=HEAD,
            pending_receipt=ok_obs,
            consecutive_zero_decision_attempts=2,
        )
        self.assertEqual(
            pkt_exh.pending_reason, "reassessment_no_progress_window_exhausted"
        )
        self.assertIn(
            "no_progress_zero_decision_window_exhausted",
            pkt_exh.offline_continuation.disqualifying_reasons,
        )

        # Validation error branches:
        with self.assertRaises(ReviewInputError):
            qualify_offline_pending_continuation("bad")  # type: ignore[arg-type]
        with self.assertRaises(ReviewInputError):
            qualify_offline_pending_continuation(
                pkt_exh.attribution, repeated_inference_calls=-1
            )
        for bad_cont in (
            {"authority": True},
            {"offline_only": False},
            {"continuation_kind": "bogus"},
            {"required_continuation_requests": -1},
            {"remaining_caller_allowance": -1},
            {
                "qualifies_zero_inference_continuation": True,
                "disqualifying_reasons": ("conflict",),
            },
        ):
            base_cont: dict[str, Any] = {
                "qualifies_zero_inference_continuation": False,
                "continuation_kind": "not_eligible_requires_new_human_evidence_or_restart",
                "zero_new_funding_verified": True,
                "zero_repeated_inference_verified": True,
                "completion_witness_established": False,
                "remaining_caller_allowance": 10,
                "required_continuation_requests": 0,
                "disqualifying_reasons": ("reason",),
            }
            base_cont.update(bad_cont)
            with self.assertRaises(ReviewInputError):
                OfflineContinuationQualification(**base_cont)

        with self.assertRaises(ReviewInputError):
            build_pending_reassessment_attempt_packet(
                identity=SessionIdentity(repository="owner/repo", pull_request=77),
                original_eligibility=orig_elig,
                latest_eligibility=orig_elig,
                bound_source_comment_id=10,
                bound_source_updated_at="t1",
                bound_source_body_sha256=source_sha,
                bound_source_kind="issue",
                bound_base_sha=BASE,
                bound_head_sha=HEAD,
                current_base_sha=BASE,
                current_head_sha=HEAD,
            )
        with self.assertRaises(ReviewInputError):
            build_pending_reassessment_attempt_packet(
                identity=id_77,
                original_eligibility=orig_elig,
                latest_eligibility=orig_elig,
                bound_source_comment_id=10,
                bound_source_updated_at="t1",
                bound_source_body_sha256=source_sha,
                bound_source_kind="issue",
                bound_base_sha=BASE,
                bound_head_sha=HEAD,
                current_base_sha=BASE,
                current_head_sha=HEAD,
                record="not-a-record",  # type: ignore[arg-type]
            )
        mismatched_rec = self._make_pending_reply_record()
        with self.assertRaisesRegex(ReviewInputError, "record identity conflicts"):
            build_pending_reassessment_attempt_packet(
                identity=id_77,
                original_eligibility=orig_elig,
                latest_eligibility=orig_elig,
                bound_source_comment_id=10,
                bound_source_updated_at="t1",
                bound_source_body_sha256=source_sha,
                bound_source_kind="issue",
                bound_base_sha=BASE,
                bound_head_sha=HEAD,
                current_base_sha=BASE,
                current_head_sha=HEAD,
                record=mismatched_rec,
            )

        qual_cont = OfflineContinuationQualification(
            qualifies_zero_inference_continuation=True,
            continuation_kind="replay_stored_pending_receipt",
            zero_new_funding_verified=True,
            zero_repeated_inference_verified=True,
            completion_witness_established=False,
            remaining_caller_allowance=10,
            required_continuation_requests=1,
            disqualifying_reasons=(),
        )
        for bad_pkt in (
            {"authority": True},
            {"offline_only": False},
            {"zero_decision_is_completion_witness": True},
            {"attribution": "bad"},
            {"case_id": "  "},
            {"repository_id": None},
            {"repository_id": 0},
            {"repository_id": -1},
            {"repository_id": True},
            {"pending_reason": "bogus"},
            {"retained_result_immutable": "bad"},
            {"admissible_to_approve": "bad"},
            {"retained_result_immutable": False},
            {
                "latest_result_digest": "9" * 64,
                "retained_result_immutable": False,
                "admissible_to_approve": True,
                "missing_proofs": (
                    "immutable_accepted_result_digest",
                    "historical_live_settlement_receipt",
                ),
            },
            {"admissible_to_approve": True},
            {"executor_sha": "not-sha"},
            {"installed_core_version": "bad version!"},
            {"pr_merged": True, "offline_continuation": qual_cont},
            {"missing_proofs": ()},
        ):
            with self.assertRaises(ReviewInputError):
                replace(pkt_exh, **bad_pkt)


if __name__ == "__main__":
    unittest.main()
