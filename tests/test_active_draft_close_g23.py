"""G23 qualification: draft and close transition timing during active work."""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import urlsplit

try:
    from fake_github_http import json_response
    from test_controlled_restart import registration as restart_registration
    from test_controlled_restart_journeys import RestartJourney
    from test_reopen_branch_lifecycle_g05 import (
        IDENTITY,
        SAMPLE_DIFF,
        LifecycleEntryFixture,
        _sample_pr_payload,
    )
    from test_reply_freshness_g33 import FreshnessReplyFixture
except ModuleNotFoundError:
    from tests.fake_github_http import json_response
    from tests.test_controlled_restart import registration as restart_registration
    from tests.test_controlled_restart_journeys import RestartJourney
    from tests.test_reopen_branch_lifecycle_g05 import (
        IDENTITY,
        SAMPLE_DIFF,
        LifecycleEntryFixture,
        _sample_pr_payload,
    )
    from tests.test_reply_freshness_g33 import FreshnessReplyFixture

from review_sensei_github.application import GitHubApplication
from review_sensei_github.branch_lifecycle import (
    ActiveTransitionKind,
    classify_active_work_state_transition,
)
from review_sensei_github.conversation import ConversationPublisher
from review_sensei_github.errors import GitHubPublicationError
from review_sensei_github.http import GitHubHttp
from review_sensei_github.publication import ReviewApprovalFinalizer, ReviewPublisher
from review_sensei_github.session_ledger import parse_session_comment

from review_sensei.api_work import ApiRunAccounting
from review_sensei.models import ReviewResult


class StateReplyFixture(FreshnessReplyFixture):
    """ReplyEntryFixture supporting live PR ``state`` and ``draft`` transitions."""

    live_state = "open"
    live_draft: object = False

    def external(self, request, timeout):
        response = super().external(request, timeout)
        target = urlsplit(request.full_url)
        if request.get_method() == "GET" and target.path.endswith("/pulls/7"):
            if response.status != 200:
                raise AssertionError(
                    f"expected 200 OK for GET {target.path}, got {response.status}"
                )
            pull = json.loads(response.read(524288))
            if not isinstance(pull, dict) or pull.get("number") != 7:
                raise AssertionError(f"unexpected PR #7 payload: {pull!r}")
            pull["state"] = self.live_state
            pull["draft"] = self.live_draft
            return json_response(pull)
        return response


def _apply_transition(
    fixture: LifecycleEntryFixture, kind: ActiveTransitionKind
) -> None:
    if kind == "converted_to_draft":
        fixture.pr_draft = True
    else:
        fixture.pr_state = "closed"


def _clear_transition(fixture: LifecycleEntryFixture) -> None:
    fixture.pr_draft = False
    fixture.pr_state = "open"


def _extract_lifecycle_event(stderr_text: str) -> dict[str, Any]:
    for line in reversed(stderr_text.splitlines()):
        prefix = "review-sensei: lifecycle "
        if line.startswith(prefix):
            payload = json.loads(line[len(prefix) :])
            assert isinstance(payload, dict)
            return payload
    raise AssertionError(f"missing lifecycle diagnostic in stderr:\n{stderr_text}")


def _recover_via_controlled_restart(
    prior: LifecycleEntryFixture, restart_dir: Path
) -> RestartJourney:
    prior_run_id = prior.run_id
    assert prior.comment is not None
    old_comment = copy.deepcopy(prior.comment)
    parts = copy.deepcopy(prior.extra_comments)
    for item in parts.values():
        item["issue_url"] = "https://api.github.test/repos/owner/repo/issues/7"
    journey = RestartJourney(restart_dir, body=old_comment["body"])
    r_fx = journey.fixture
    r_fx.run_id = prior_run_id + 10
    r_fx.live_head = r_fx.requested_head = prior.live_head
    r_fx.extra_comments = copy.deepcopy(parts)
    archived = [journey.old, *parts.values()]
    journey.row = restart_registration(
        policy_commit=r_fx.policy,
        workflow_sha="a" * 40,
        base_sha=r_fx.live_base,
        head_sha=prior.live_head,
        run_id=str(r_fx.run_id),
        job="hosted",
        old_runs=[prior_run_id],
        old_comments=[
            {
                "id": item["id"],
                "bytes": len(item["body"].encode()),
                "sha256": hashlib.sha256(item["body"].encode()).hexdigest(),
            }
            for item in archived
        ],
    )

    def failed_prior_owner(request, timeout, original):
        response = original(request, timeout)
        path = urlsplit(request.full_url).path
        if path == f"/repos/owner/repo/actions/runs/{prior_run_id}":
            value = json.loads(response.read(512 * 1024 + 1))
            value.update(conclusion="failure", head_sha=prior.live_head)
            return json_response(value)
        if path == f"/repos/owner/repo/actions/runs/{prior_run_id}/attempts/1/jobs":
            value = json.loads(response.read(512 * 1024 + 1))
            for job in value["jobs"]:
                job["conclusion"] = "failure"
            return json_response(value)
        return response

    journey.external_hook = failed_prior_owner
    return journey


class ActiveDraftCloseG23Tests(unittest.TestCase):
    """End-to-end qualification of draft/close timing across all 5 execution phases."""

    def test_classifier_contract_and_validation_across_all_phases(self) -> None:
        kinds: tuple[ActiveTransitionKind, ...] = ("converted_to_draft", "closed")
        for kind in kinds:
            d1 = classify_active_work_state_transition(
                phase="before_inference", transition_kind=kind
            )
            self.assertFalse(d1.provider_cost_incurred)
            self.assertTrue(d1.caller_dispatches_retained_without_refund)
            self.assertTrue(d1.newly_authorized_writes_blocked)
            self.assertFalse(d1.already_dispatched_effects_revoked)
            self.assertFalse(d1.accounting_terminal)
            self.assertIsNone(d1.expected_transaction_phase)
            self.assertEqual(d1.recovery_route, "fresh_run_when_ready_or_reopened")

            d2 = classify_active_work_state_transition(
                phase="during_provider_work", transition_kind=kind
            )
            self.assertTrue(d2.provider_cost_incurred)
            self.assertTrue(d2.caller_dispatches_retained_without_refund)
            self.assertFalse(d2.accounting_terminal)
            self.assertEqual(
                d2.expected_transaction_phase,
                None if kind == "closed" else "publication_failed",
            )
            self.assertEqual(
                d2.publication_skip_status,
                None if kind == "closed" else "skipped_pr_state",
            )
            self.assertEqual(
                d2.recovery_route, "controlled_restart_when_ready_or_reopened"
            )

            d3 = classify_active_work_state_transition(
                phase="after_accepted_result", transition_kind=kind
            )
            self.assertTrue(d3.provider_cost_incurred)
            self.assertEqual(d3.publication_skip_status, "skipped_pr_state")
            self.assertEqual(d3.expected_transaction_phase, "publication_failed")
            self.assertFalse(d3.accounting_terminal)
            self.assertEqual(d3.terminal_reason, "publication_finalization_incomplete")
            self.assertEqual(
                d3.recovery_route, "controlled_restart_when_ready_or_reopened"
            )

            d4 = classify_active_work_state_transition(
                phase="before_final_preflight", transition_kind=kind
            )
            self.assertTrue(d4.provider_cost_incurred)
            self.assertEqual(d4.publication_skip_status, "skipped_pr_state")
            self.assertFalse(d4.accounting_terminal)
            self.assertFalse(d4.duplicate_inference_on_recovery)
            self.assertEqual(
                d4.recovery_route, "same_run_retained_replay_zero_inference"
            )

            d5_ok = classify_active_work_state_transition(
                phase="after_dispatch",
                transition_kind=kind,
                dispatch_effect_state="confirmed_review_before_approval",
            )
            self.assertTrue(d5_ok.provider_cost_incurred)
            self.assertTrue(d5_ok.newly_authorized_writes_blocked)
            self.assertFalse(d5_ok.already_dispatched_effects_revoked)
            self.assertFalse(d5_ok.uncertain_dispatch_effect_preserved)
            self.assertTrue(d5_ok.accounting_terminal)
            self.assertEqual(d5_ok.expected_transaction_phase, "publication_succeeded")
            self.assertEqual(
                d5_ok.recovery_route,
                "reuse_published_review_and_reevaluate_approval_zero_inference",
            )

            d5_stored = classify_active_work_state_transition(
                phase="after_dispatch",
                transition_kind=kind,
                dispatch_effect_state="lost_response_stored",
            )
            self.assertTrue(d5_stored.uncertain_dispatch_effect_preserved)
            self.assertFalse(d5_stored.accounting_terminal)
            self.assertEqual(d5_stored.terminal_reason, "terminal_close_unconfirmed")
            self.assertEqual(
                d5_stored.recovery_route, "hold_uncertain_effect_until_reconciled"
            )

            d5_unconf = classify_active_work_state_transition(
                phase="after_dispatch",
                transition_kind=kind,
                dispatch_effect_state="lost_response_unconfirmed",
            )
            self.assertTrue(d5_unconf.uncertain_dispatch_effect_preserved)
            self.assertFalse(d5_unconf.accounting_terminal)
            self.assertEqual(d5_unconf.expected_transaction_phase, "publication_failed")
            self.assertEqual(
                d5_unconf.recovery_route, "hold_uncertain_effect_until_reconciled"
            )

        with self.assertRaisesRegex(
            GitHubPublicationError, "active work transition phase is invalid"
        ):
            classify_active_work_state_transition(
                phase="unknown",  # type: ignore[arg-type]
                transition_kind="closed",
            )
        with self.assertRaisesRegex(
            GitHubPublicationError, "active work transition kind is invalid"
        ):
            classify_active_work_state_transition(
                phase="before_inference",
                transition_kind="merged",  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(
            GitHubPublicationError, "dispatch effect state is invalid"
        ):
            classify_active_work_state_transition(
                phase="before_inference",
                transition_kind="closed",
                dispatch_effect_state=123,  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(
            GitHubPublicationError,
            "pre-dispatch phases require dispatch_effect_state='not_dispatched'",
        ):
            classify_active_work_state_transition(
                phase="before_final_preflight",
                transition_kind="converted_to_draft",
                dispatch_effect_state="confirmed_review_before_approval",
            )
        with self.assertRaisesRegex(
            GitHubPublicationError,
            "after_dispatch requires an explicit post-dispatch effect state",
        ):
            classify_active_work_state_transition(
                phase="after_dispatch",
                transition_kind="closed",
                dispatch_effect_state="not_dispatched",
            )

    def test_phase_1_before_inference_blocks_provider_retains_startup_dispatches_and_recovers(
        self,
    ) -> None:
        """Phase 1 (before_inference): draft and close on review and reply routes."""

        for kind in ("converted_to_draft", "closed"):
            with (
                self.subTest(route="review", kind=kind),
                tempfile.TemporaryDirectory() as tmp,
            ):
                fx = LifecycleEntryFixture(Path(tmp))
                _apply_transition(fx, kind)
                code_skip, out_skip = fx.run_and_read(human=False)
                self.assertEqual(code_skip, 1)
                self.assertIsNone(out_skip)
                self.assertEqual(fx.provider_calls, 0)
                self.assertEqual(fx.review_posts, [])
                lifecycle = _extract_lifecycle_event(fx.error)
                self.assertEqual(lifecycle["phase"], "bootstrap")
                self.assertGreater(lifecycle["caller"]["observed_dispatches"], 0)

                # Once ready/reopened, a fresh run completes cleanly with 1 inference.
                _clear_transition(fx)
                fx.advance_run_same_snapshot()
                code_ok, out_ok = fx.run_and_read(human=False)
                self.assertEqual(code_ok, 0, fx.error)
                assert out_ok is not None
                self.assertEqual(out_ok["status"], "published")
                self.assertTrue(out_ok["accounting_terminal"])
                self.assertEqual(fx.provider_calls, 1)
                self.assertEqual(
                    [r["event"] for r in fx.review_posts], ["COMMENT", "APPROVE"]
                )

            with (
                self.subTest(route="reply_prepare_context", kind=kind),
                tempfile.TemporaryDirectory() as rtmp,
            ):
                reply_fx = StateReplyFixture(Path(rtmp))
                reply_fx.initialize_freshness()
                original_prepare = ConversationPublisher.prepare_context

                def transition_during_prepare(publisher, **kwargs):
                    if kind == "converted_to_draft":
                        reply_fx.live_draft = True
                    else:
                        reply_fx.live_state = "closed"
                    return original_prepare(publisher, **kwargs)

                with patch.object(
                    ConversationPublisher, "prepare_context", transition_during_prepare
                ):
                    reply_code = reply_fx.invoke_reply()
                self.assertEqual(reply_code, 1)
                self.assertEqual(reply_fx.provider_calls, 0)
                self.assertEqual(reply_fx.reply_posts, 0)
                self.assertEqual(
                    getattr(reply_fx.last_result, "status", None), "skipped_pr_state"
                )
                self.assertEqual(
                    len(reply_fx.accounted.observations), len(reply_fx.trace)
                )
                self.assertEqual(reply_fx.action()["phase"], "pending")

    def test_phase_2_during_provider_work_retains_cost_blocks_writes_and_recovers_via_restart(
        self,
    ) -> None:
        """Phase 2 (during_provider_work): draft and close during Provider.complete."""

        for kind in ("converted_to_draft", "closed"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "prior").mkdir()
                (root / "restart").mkdir()
                fx = LifecycleEntryFixture(root / "prior")
                fx.before_provider = lambda k=kind: _apply_transition(fx, k)
                code, out = fx.run_and_read(human=False)
                self.assertEqual(code, 1)
                self.assertEqual(fx.provider_calls, 1)
                self.assertEqual(fx.review_posts, [])
                lifecycle = _extract_lifecycle_event(fx.error)
                self.assertTrue(lifecycle["caller"]["accounting_available"])
                self.assertGreater(lifecycle["caller"]["recorded_completed"], 0)
                self.assertIsNone(lifecycle["caller"]["review_terminal_transaction"])

                assert fx.comment is not None
                record = parse_session_comment(fx.comment["body"], identity=IDENTITY)
                if kind == "closed":
                    self.assertIsNone(out)
                    self.assertIsNone(record.transaction)
                    assert isinstance(record.api_run_accounting, dict)
                    self.assertIsNone(
                        record.api_run_accounting.get("terminal_transaction")
                    )
                    self.assertIn(
                        "retention current pull request identity mismatches", fx.error
                    )
                else:
                    assert out is not None
                    self.assertEqual(out["status"], "skipped_pr_state")
                    self.assertFalse(out["accounting_terminal"])
                    self.assertEqual(
                        out["terminal_reason"], "publication_finalization_incomplete"
                    )
                    assert record.transaction is not None
                    self.assertEqual(record.transaction.phase, "publication_failed")

                # Unauthenticated new-run rollover fails closed with zero new inference.
                fx.before_provider = None
                _clear_transition(fx)
                prior_run_id = fx.run_id
                fx.advance_run_same_snapshot()
                code_hold, _ = fx.run_and_read(human=False)
                self.assertEqual(code_hold, 1)
                self.assertIn("previous caller run is not terminal", fx.error)
                self.assertEqual(fx.provider_calls, 1)
                self.assertEqual(fx.review_posts, [])

                # Restore prior_run_id for restart registration and recover cleanly.
                fx.run_id = prior_run_id
                journey = _recover_via_controlled_restart(fx, root / "restart")
                self.assertEqual(journey.invoke(human=False), 0, journey.fixture.error)
                self.assertEqual(journey.fixture.provider_calls, 1)
                self.assertIsNotNone(
                    journey.fixture.selected.caller.accounting.terminal_transaction
                )
                self.assertEqual(journey.fixture.reviews[-1]["commit_id"], fx.live_head)

    def test_phase_3_after_accepted_result_gates_writes_and_replays_accepted_reply_without_reinference(
        self,
    ) -> None:
        """Phase 3 (after_accepted_result): accepted review and reply results before staging."""

        for kind in ("converted_to_draft", "closed"):
            with (
                self.subTest(route="review", kind=kind),
                tempfile.TemporaryDirectory() as tmp,
            ):
                root = Path(tmp)
                (root / "prior").mkdir()
                (root / "restart").mkdir()
                fx = LifecycleEntryFixture(root / "prior")
                original_preflight = ReviewPublisher._preflight_pr
                preflight_calls = 0

                def transition_after_accepted_result(publisher, **kwargs):
                    nonlocal preflight_calls
                    preflight_calls += 1
                    if preflight_calls == 1:
                        _apply_transition(fx, kind)
                    return original_preflight(publisher, **kwargs)

                with patch.object(
                    ReviewPublisher, "_preflight_pr", transition_after_accepted_result
                ):
                    code, out = fx.run_and_read(human=False)
                self.assertEqual(code, 1)
                assert out is not None
                self.assertEqual(out["status"], "skipped_pr_state")
                self.assertFalse(out["accounting_terminal"])
                self.assertEqual(
                    out["terminal_reason"], "publication_finalization_incomplete"
                )
                self.assertEqual(fx.provider_calls, 1)
                self.assertEqual(fx.review_posts, [])
                assert fx.comment is not None
                record = parse_session_comment(fx.comment["body"], identity=IDENTITY)
                assert record.transaction is not None
                self.assertEqual(record.transaction.phase, "publication_failed")

                _clear_transition(fx)
                journey = _recover_via_controlled_restart(fx, root / "restart")
                self.assertEqual(journey.invoke(human=False), 0, journey.fixture.error)
                self.assertIsNotNone(
                    journey.fixture.selected.caller.accounting.terminal_transaction
                )

            with (
                self.subTest(route="reply_accepted_reentry", kind=kind),
                tempfile.TemporaryDirectory() as rtmp,
            ):
                reply_fx = StateReplyFixture(Path(rtmp))
                reply_fx.initialize_freshness()
                original_gen = GitHubApplication.generate_and_publish_reply

                def transition_on_provider(_req):
                    if kind == "converted_to_draft":
                        reply_fx.live_draft = True
                    else:
                        reply_fx.live_state = "closed"
                    return reply_fx.reply_document

                reply_fx.provider_callback = transition_on_provider

                def skip_then_reenter_when_ready(application, **arguments):
                    first = original_gen(application, **arguments)
                    self.assertEqual(first.status, "skipped_pr_state")
                    self.assertEqual(reply_fx.provider_calls, 1)
                    self.assertEqual(reply_fx.reply_posts, 0)
                    # Clear draft/closed state and re-enter: must publish accepted reply
                    # with ZERO duplicate model inference!
                    reply_fx.live_draft = False
                    reply_fx.live_state = "open"
                    return original_gen(application, **arguments)

                with patch.object(
                    GitHubApplication,
                    "generate_and_publish_reply",
                    skip_then_reenter_when_ready,
                ):
                    code_reply = reply_fx.invoke_reply()
                self.assertEqual(code_reply, 0, reply_fx.error)
                self.assertEqual(reply_fx.provider_calls, 1)
                self.assertEqual(reply_fx.reply_posts, 1)
                self.assertEqual(reply_fx.action()["phase"], "terminal")
                self.assertEqual(
                    len(reply_fx.accounted.observations), len(reply_fx.trace)
                )

    def test_phase_4_before_final_preflight_blocks_mutations_and_recovers_attempt_2_zero_inference(
        self,
    ) -> None:
        """Phase 4 (before_final_preflight): final pre-mutation fence + zero-inference replay."""

        for kind in ("converted_to_draft", "closed"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                fx = LifecycleEntryFixture(Path(tmp))
                original_preflight = ReviewPublisher._preflight_pr
                transitioned = False

                def transition_on_final_preflight(publisher, **kwargs):
                    nonlocal transitioned
                    if not transitioned and any(
                        "default-human-inventory" in str(item.get("body", ""))
                        for item in fx.extra_comments.values()
                    ):
                        transitioned = True
                        _apply_transition(fx, kind)
                    return original_preflight(publisher, **kwargs)

                with patch.object(
                    ReviewPublisher, "_preflight_pr", transition_on_final_preflight
                ):
                    code_1, out_1 = fx.run_and_read(human=False)
                self.assertTrue(transitioned)
                self.assertEqual(code_1, 1)
                assert out_1 is not None
                self.assertEqual(out_1["status"], "skipped_pr_state")
                self.assertFalse(out_1["accounting_terminal"])
                self.assertEqual(
                    out_1["terminal_reason"], "publication_finalization_incomplete"
                )
                self.assertEqual(fx.provider_calls, 1)
                self.assertEqual(fx.review_posts, [])
                lifecycle_1 = _extract_lifecycle_event(fx.error)
                self.assertGreater(lifecycle_1["caller"]["recorded_completed"], 0)
                self.assertIsNone(lifecycle_1["caller"]["review_terminal_transaction"])

                # Same-run attempt 2 once ready/reopened publishes from retained analysis
                # with ZERO duplicate model inference (`provider_calls` stays 1)!
                _clear_transition(fx)
                fx.attempt = 2
                fx.trace.clear()
                fx.transports.clear()
                fx.worker_observations.clear()
                code_2, out_2 = fx.run_and_read(human=False)
                self.assertEqual(code_2, 0, fx.error)
                assert out_2 is not None
                self.assertEqual(out_2["status"], "published")
                self.assertTrue(out_2["accounting_terminal"])
                self.assertIsNone(out_2["terminal_reason"])
                self.assertEqual(fx.provider_calls, 1)
                self.assertEqual(
                    [r["event"] for r in fx.review_posts], ["COMMENT", "APPROVE"]
                )

    def test_phase_5_after_dispatch_confirmed_review_and_uncertain_lost_response_contracts(
        self,
    ) -> None:
        """Phase 5 (after_dispatch): post-COMMENT gate AND lost/uncertain HTTP response."""

        # Part A: Deterministic transition after COMMENT review POST (`200 OK`)
        # before APPROVE finalization preflight.
        for kind in ("converted_to_draft", "closed"):
            with (
                self.subTest(subcase="confirmed_review_before_approval", kind=kind),
                tempfile.TemporaryDirectory() as tmp,
            ):

                class PostCommentStateFixture(LifecycleEntryFixture):
                    armed_kind: ActiveTransitionKind | None = None

                    def external(self, request, timeout):
                        resp = super().external(request, timeout)
                        path = urlsplit(request.full_url).path
                        if (
                            self.armed_kind is not None
                            and request.get_method() == "POST"
                            and path.endswith("/pulls/7/reviews")
                            and json.loads(request.data).get("event") == "COMMENT"
                            and resp.status == 200
                        ):
                            _apply_transition(self, self.armed_kind)
                        return resp

                fx = PostCommentStateFixture(Path(tmp))
                fx.armed_kind = kind
                code_1, out_1 = fx.run_and_read(human=False)
                self.assertEqual(code_1, 0, fx.error)
                assert out_1 is not None
                self.assertEqual(out_1["status"], "published")
                self.assertTrue(out_1["accounting_terminal"])
                self.assertEqual(fx.provider_calls, 1)
                # COMMENT review stays intact; APPROVE was blocked as skipped_pr_state.
                self.assertEqual([r["event"] for r in fx.review_posts], ["COMMENT"])

                # Ready/reopen same-snapshot run reuses published review with ZERO inference.
                fx.armed_kind = None
                _clear_transition(fx)
                fx.advance_run_same_snapshot()
                code_2, out_2 = fx.run_and_read(human=False)
                self.assertEqual(code_2, 0, fx.error)
                assert out_2 is not None
                self.assertEqual(out_2["status"], "already_published")
                self.assertTrue(out_2["accounting_terminal"])
                self.assertEqual(fx.provider_calls, 1)

                # Explicit approval finalization from persisted eligibility now emits APPROVE
                # without any provider call.
                finalizer = ReviewApprovalFinalizer(http=fx.http)
                persisted = finalizer.load_eligibility(
                    token="installation-token",
                    repository="owner/repo",
                    pull_request=7,
                    head_sha=fx.live_head,
                    app_slug="reviewsensei[bot]",
                    require_valid=True,
                )
                self.assertIsNotNone(persisted)
                assert persisted is not None
                approved = finalizer.finalize(
                    token="installation-token",
                    repository="owner/repo",
                    pull_request=7,
                    head_sha=fx.live_head,
                    app_slug="reviewsensei[bot]",
                    eligibility=persisted,
                )
                self.assertEqual(approved.status, "approved")
                self.assertEqual(fx.provider_calls, 1)
                self.assertEqual(
                    [r["state"] for r in fx.reviews], ["COMMENTED", "APPROVED"]
                )

        # Part B: Lost/uncertain HTTP response (`TimeoutError`) on review POST
        # concurrent with `draft=True` and `state="closed"` (both stored and absent).
        for kind in ("converted_to_draft", "closed"):
            for stored in (False, True):
                with (
                    self.subTest(
                        subcase="lost_review_post_response", kind=kind, stored=stored
                    ),
                    tempfile.TemporaryDirectory() as tmp_u,
                ):

                    class UncertainDispatchFixture(LifecycleEntryFixture):
                        inject_loss = True

                        def external(self, request, timeout):
                            path = urlsplit(request.full_url).path
                            if (
                                self.inject_loss
                                and request.get_method() == "POST"
                                and path.endswith("/pulls/7/reviews")
                            ):
                                self.inject_loss = False
                                _apply_transition(self, kind)
                                if stored:
                                    super().external(request, timeout)
                                else:
                                    self.trace.append(
                                        (self.attempt, request.get_method(), path)
                                    )
                                raise TimeoutError(
                                    "synthetic lost review POST response"
                                )
                            return super().external(request, timeout)

                    ufx = UncertainDispatchFixture(Path(tmp_u))
                    code_u, out_u = ufx.run_and_read(human=False)
                    self.assertEqual(code_u, 1, f"{out_u!r}\n{ufx.error}")
                    self.assertEqual(ufx.provider_calls, 1)
                    lifecycle_u = _extract_lifecycle_event(ufx.error)
                    self.assertIsNone(
                        lifecycle_u["caller"]["review_terminal_transaction"]
                    )
                    self.assertGreaterEqual(
                        lifecycle_u["caller"]["recorded_pending"], 1
                    )
                    # Zero APPROVE reviews emitted in all uncertain-dispatch cases.
                    self.assertNotIn("APPROVE", [r["event"] for r in ufx.review_posts])

                    if stored:
                        assert out_u is not None
                        # Reconciled stored COMMENT without duplicating it, while
                        # truthfully holding accounting_terminal=False over the
                        # unconfirmed physical POST!
                        self.assertEqual(out_u["status"], "published_terminal_pending")
                        self.assertEqual(
                            out_u["publication_status"], "already_published"
                        )
                        self.assertFalse(out_u["accounting_terminal"])
                        self.assertEqual(
                            out_u["terminal_reason"], "terminal_close_unconfirmed"
                        )
                        self.assertEqual(
                            [r["event"] for r in ufx.review_posts], ["COMMENT"]
                        )
                    else:
                        # The durable journal now reports the unknown outcome
                        # explicitly; it does not turn absence into completion.
                        self.assertEqual(
                            out_u,
                            {
                                "status": "publication_incomplete",
                                "publication_status": "unknown",
                                "accounting_terminal": False,
                                "terminal_reason": "publication_interrupted",
                                "review_id": None,
                                "diagnostic": "publication_outcome_incomplete",
                            },
                        )
                        self.assertEqual(ufx.review_posts, [])
                        assert ufx.comment is not None
                        record_u = parse_session_comment(
                            ufx.comment["body"], identity=IDENTITY
                        )
                        assert isinstance(record_u.api_run_accounting, dict)
                        self.assertIsNone(
                            record_u.api_run_accounting.get("terminal_transaction")
                        )

    def test_strict_draft_boolean_validation_g24_across_all_active_work_entry_and_preflight_routes(
        self,
    ) -> None:
        """G23/G24 exact-scope: non-boolean/missing `draft` fails closed without truthiness coercion."""

        malformed_drafts: tuple[tuple[str, object, bool], ...] = (
            ("none", None, False),
            ("int_zero", 0, False),
            ("int_one", 1, False),
            ("str_false", "false", False),
            ("str_true", "true", False),
            ("missing_key", False, True),
        )

        # 1. Direct publisher & approval-finalizer preflights reject every malformed `draft`.
        for label, bad_val, delete_key in malformed_drafts:
            with self.subTest(route="publisher_and_finalizer", case=label):
                payload = _sample_pr_payload(draft=bad_val)
                if delete_key:
                    del payload["draft"]

                http = GitHubHttp(
                    api_url="https://api.github.test",
                    opener=lambda _req, timeout=None, p=payload: json_response(p),
                )
                with self.assertRaisesRegex(
                    GitHubPublicationError, "review preflight draft was invalid"
                ):
                    ReviewPublisher(http=http).publish(
                        token="installation-token",
                        repository="owner/repo",
                        repository_id=123,
                        pull_request=7,
                        head_sha="a" * 40,
                        base_branch="main",
                        base_sha="b" * 40,
                        diff=SAMPLE_DIFF,
                        result=ReviewResult(
                            summary="Clean result.",
                            comments=(),
                            provider="ollama",
                            review_status="complete",
                        ),
                        app_slug="reviewsensei[bot]",
                    )
                with self.assertRaisesRegex(
                    GitHubPublicationError,
                    "approval finalization preflight draft was invalid",
                ):
                    ReviewApprovalFinalizer(http=http)._preflight(
                        token="installation-token",
                        repository="owner/repo",
                        pull_request=7,
                        head_sha="a" * 40,
                        app_slug="reviewsensei[bot]",
                    )

        # 2. Live ReviewRun bootstrap (`before_inference`) and ConversationPublisher reply
        # routes fail closed on malformed `draft` without invoking the provider or posting writes.
        for bad_val in (0, "false", None):
            with (
                self.subTest(route="review_run_bootstrap", draft=bad_val),
                tempfile.TemporaryDirectory() as tmp_r,
            ):
                fx = LifecycleEntryFixture(Path(tmp_r))
                fx.pr_draft = bad_val
                code, out = fx.run_and_read(human=False)
                self.assertEqual(code, 1)
                self.assertIsNone(out)
                self.assertEqual(fx.provider_calls, 0)
                self.assertEqual(fx.review_posts, [])

            with (
                self.subTest(route="reply_post_inference_preflight", draft=bad_val),
                tempfile.TemporaryDirectory() as tmp_c,
            ):
                reply_fx = StateReplyFixture(Path(tmp_c))
                reply_fx.initialize_freshness()
                orig_pub = ConversationPublisher._publish

                def corrupt_draft_before_reply(publisher, **kwargs):
                    reply_fx.live_draft = bad_val
                    return orig_pub(publisher, **kwargs)

                with patch.object(
                    ConversationPublisher, "_publish", corrupt_draft_before_reply
                ):
                    code_reply = reply_fx.invoke_reply()
                self.assertEqual(code_reply, 1)
                self.assertEqual(reply_fx.reply_posts, 0)
                self.assertIsNone(reply_fx.last_result)
                self.assertEqual(reply_fx.action()["phase"], "pending")

    def test_caller_request_cost_and_unknown_effect_conservation_across_active_work_transitions(
        self,
    ) -> None:
        """G23 exact-scope: ApiRunAccounting conservation, non-refund, and retained work."""

        def assert_conserved(accounting: ApiRunAccounting) -> None:
            self.assertGreaterEqual(accounting.remaining, 0)
            self.assertEqual(
                accounting.startup.liability
                + accounting.prior_nonstartup_liability
                + accounting.confirmed
                + accounting.uncertain
                + accounting.reserved_undispatched
                + accounting.remaining,
                accounting.configuration.limit,
            )

        # 1. `before_final_preflight` -> `attempt=2` retained replay:
        # Attempt 1 incurs 1 provider call and N_1 confirmed caller HTTP requests;
        # Attempt 2 reuses the exact retained `result_sha256` with 0 new provider calls,
        # never refunds N_1 (`confirmed_2 > confirmed_1`), and settles terminally.
        with tempfile.TemporaryDirectory() as tmp:
            fx = LifecycleEntryFixture(Path(tmp))
            orig_preflight = ReviewPublisher._preflight_pr
            transitioned = False

            def draft_on_final_preflight(publisher, **kwargs):
                nonlocal transitioned
                if not transitioned and any(
                    "default-human-inventory" in str(item.get("body", ""))
                    for item in fx.extra_comments.values()
                ):
                    transitioned = True
                    fx.pr_draft = True
                return orig_preflight(publisher, **kwargs)

            with patch.object(
                ReviewPublisher, "_preflight_pr", draft_on_final_preflight
            ):
                code_1, out_1 = fx.run_and_read(human=False)
            self.assertEqual(code_1, 1)
            assert out_1 is not None
            self.assertEqual(out_1["status"], "skipped_pr_state")
            self.assertEqual(fx.provider_calls, 1)

            assert fx.comment is not None
            rec_1 = parse_session_comment(fx.comment["body"], identity=IDENTITY)
            assert rec_1 is not None and rec_1.transaction is not None
            assert isinstance(rec_1.api_run_accounting, dict)
            acc_1 = ApiRunAccounting.from_document(rec_1.api_run_accounting)
            assert_conserved(acc_1)
            self.assertGreater(acc_1.confirmed, 0)
            self.assertEqual(acc_1.uncertain, 0)
            self.assertIsNone(acc_1.terminal_transaction)
            retained_digest_1 = rec_1.transaction.result_sha256
            self.assertIsNotNone(retained_digest_1)

            # Ready for review -> attempt=2 replays accepted work without refunding attempt=1 cost.
            fx.pr_draft = False
            fx.attempt = 2
            fx.trace.clear()
            fx.transports.clear()
            fx.worker_observations.clear()
            code_2, out_2 = fx.run_and_read(human=False)
            self.assertEqual(code_2, 0, fx.error)
            assert out_2 is not None
            self.assertTrue(out_2["accounting_terminal"])
            self.assertEqual(fx.provider_calls, 1)

            rec_2 = parse_session_comment(fx.comment["body"], identity=IDENTITY)
            assert rec_2 is not None and rec_2.transaction is not None
            assert isinstance(rec_2.api_run_accounting, dict)
            acc_2 = ApiRunAccounting.from_document(rec_2.api_run_accounting)
            assert_conserved(acc_2)
            self.assertGreater(acc_2.confirmed, acc_1.confirmed)
            self.assertEqual(acc_2.uncertain, 0)
            self.assertEqual(rec_2.transaction.result_sha256, retained_digest_1)
            self.assertEqual(
                acc_2.terminal_transaction, rec_2.transaction.transaction_id
            )

        # 2. `after_dispatch` with unknown HTTP effect (`TimeoutError` concurrent with `closed`):
        # Persisted `ApiRunAccounting` retains `uncertain >= 1`, `remaining >= 0`, exact
        # budget conservation, accepted `result_sha256`, and `terminal_transaction is None`.
        with tempfile.TemporaryDirectory() as tmp_u:

            class _UnknownDispatchCloseFixture(LifecycleEntryFixture):
                armed = True

                def external(self, request, timeout):
                    path = urlsplit(request.full_url).path
                    if (
                        self.armed
                        and request.get_method() == "POST"
                        and path.endswith("/pulls/7/reviews")
                    ):
                        self.armed = False
                        self.pr_state = "closed"
                        self.trace.append((self.attempt, "POST", path))
                        raise TimeoutError("lost review POST concurrent with PR close")
                    return super().external(request, timeout)

            ufx = _UnknownDispatchCloseFixture(Path(tmp_u))
            code_u, out_u = ufx.run_and_read(human=False)
            self.assertEqual(code_u, 1)
            self.assertEqual(
                out_u,
                {
                    "status": "publication_incomplete",
                    "publication_status": "unknown",
                    "accounting_terminal": False,
                    "terminal_reason": "publication_interrupted",
                    "review_id": None,
                    "diagnostic": "publication_outcome_incomplete",
                },
            )
            self.assertEqual(ufx.provider_calls, 1)
            self.assertEqual(ufx.review_posts, [])

            assert ufx.comment is not None
            rec_u = parse_session_comment(ufx.comment["body"], identity=IDENTITY)
            assert rec_u is not None and rec_u.transaction is not None
            assert isinstance(rec_u.api_run_accounting, dict)
            acc_u = ApiRunAccounting.from_document(rec_u.api_run_accounting)
            assert_conserved(acc_u)
            self.assertGreaterEqual(acc_u.uncertain, 1)
            self.assertGreater(acc_u.confirmed, 0)
            self.assertIsNone(acc_u.terminal_transaction)
            self.assertEqual(rec_u.transaction.phase, "publication_failed")
            self.assertIsNotNone(rec_u.transaction.result_sha256)


if __name__ == "__main__":
    unittest.main()
