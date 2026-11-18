"""Fresh human progress preserves independently authenticated carried holds."""

import itertools
import tempfile
import unittest
from copy import deepcopy
from dataclasses import replace
from unittest.mock import patch

from review_sensei_github.publication import (
    approval_eligibility_from_body,
    approval_eligibility_marker,
)
from test_carried_assessment_composition import carried_views
from test_human_assessment import State, prior

from review_sensei.errors import ReviewInputError


class CarriedHumanProgressTests(unittest.TestCase):
    def test_application_reassessment_keeps_pending_and_settled_historical_holds(self):
        from review_sensei_github.application import GitHubApplication
        from review_sensei_github.session_ledger import parse_session_comment
        from test_action_reply_entry import ReplyEntryFixture
        from test_action_run import HistoricalCommandFixture, historical_body
        from test_review_run import (
            IDENTITY,
            ConsecutiveReviewRunTests,
            _CompletionFixture,
        )

        class HistoricalReplyFixture(ReplyEntryFixture, _CompletionFixture):
            """Reply endpoints followed by original historical metadata endpoints.

            Only external synthetic HTTP is composed. Production readers,
            finalizer methods, archive verification and caller ownership remain
            the original implementations.
            """

            def external(self, request, timeout):
                import json
                from urllib.parse import urlsplit

                from fake_github_http import json_response

                response = super().external(request, timeout)
                path = urlsplit(request.full_url).path
                # Retain the command helper's external predecessor-job truth
                # after switching to reply endpoint dispatch.
                if "/actions/runs/" in path and path.endswith("/jobs"):
                    run_id = int(path.split("/actions/runs/")[1].split("/")[0])
                    if run_id in self.past_jobs:
                        value = json.loads(response.read(524288))
                        value["jobs"][0]["name"] = (
                            "ReviewSensei / " + self.past_jobs[run_id]
                        )
                        return json_response(value)
                return response

            def invoke_reply(self):
                if self.legacy_publication_fixture:
                    with patch.object(
                        GitHubApplication,
                        "_bind_finding_lifecycle_finalizer",
                        return_value=None,
                    ):
                        return super().invoke_reply()
                return super().invoke_reply()

        for pending in (False, True):
            with (
                self.subTest(pending=pending),
                tempfile.TemporaryDirectory() as directory,
            ):
                fixture = ConsecutiveReviewRunTests.historical_case(
                    directory,
                    lambda path, mode: HistoricalCommandFixture(
                        path,
                        mode,
                        comments=[
                            {
                                "path": "src/app.py",
                                "line": 2,
                                "side": "RIGHT",
                                "body": "Confirm the earlier historical migration invariant.",
                                "evidence_id": "historical-migration-contract",
                                "severity": "high",
                                "blocking": False,
                                "needs_human": True,
                            }
                        ],
                    ),
                )
                fixture.provider_comments = None
                self.assertEqual(fixture.invoke(human=True), 0, fixture.error)
                record = parse_session_comment(
                    fixture.comment["body"], identity=IDENTITY
                )
                original_hold = deepcopy(record.historical_assessments)
                if not pending:
                    for item in original_hold["assessments"]:
                        fixture.SOURCE_ID += 1
                        fixture.select_command(
                            historical_body(original_hold, instance=item["instance"]),
                            new_run=True,
                        )
                        before = fixture.provider_calls
                        self.assertEqual(fixture.invoke_command(), 0, fixture.error)
                        self.assertEqual(fixture.provider_calls, before)
                        # The command adapter served this original comment from
                        # its selected source slot. Preserve it as external API
                        # data after changing dispatch adapters.
                        source_id = fixture.source["id"]
                        if source_id in fixture.extra_comments:
                            self.assertEqual(
                                fixture.extra_comments[source_id], fixture.source
                            )
                        fixture.extra_comments[source_id] = deepcopy(fixture.source)
                record = parse_session_comment(
                    fixture.comment["body"], identity=IDENTITY
                )
                historical = deepcopy(record.historical_assessments)
                self.assertEqual(historical["source"], original_hold["source"])
                self.assertEqual(
                    len(historical["settlements"]),
                    0 if pending else len(original_hold["assessments"]),
                )
                original = next(
                    value
                    for review in reversed(fixture.reviews)
                    if (value := approval_eligibility_from_body(review["body"]))
                    is not None
                )
                self.assertTrue(original.fresh_human_pending)
                self.assertTrue(original.human_review.pending)
                self.assertEqual(original.facts.review_status, "complete")
                view = original.historical_assessments

                # Explicit external/configuration DATA transfer only. No old
                # caller, ledger, HTTP adapter, selected run or callbacks enter
                # the new reply bootstrap. Archive and root bytes stay intact.
                reply = HistoricalReplyFixture.__new__(HistoricalReplyFixture)
                external_fields = (
                    "directory",
                    "root",
                    "fanout",
                    "plan",
                    "limit",
                    "attempt",
                    "comment",
                    "extra_comments",
                    "reviews",
                    "inline",
                    "threads",
                    "check_runs",
                    "config_bytes",
                    "policy",
                    "policy_ref",
                    "live_head",
                    "live_base",
                    "requested_head",
                    "requested_base",
                    "run_id",
                    "owner_job",
                    "past_jobs",
                    "run_results",
                    "run_heads",
                    "qualified_old_soft_cleanup_successes",
                    "history_count",
                    "provider_calls",
                    "quarantine_predecessor_comment_id",
                    "legacy_publication_fixture",
                    "mode",
                    "fault",
                    "old_active",
                    "incomplete_jobs",
                    "changed_old_job",
                    "oversized_first_page",
                    "interrupt_engine_read",
                    "fail_terminal_patch",
                    "overlap",
                    "refuse_broker",
                    "previous_active",
                    "previous_incomplete",
                    "rollover_fault",
                    "change_root",
                    "archive_bad",
                    "abort_fault",
                    "completion_fault",
                    "terminal_write_fault",
                )
                for name in external_fields:
                    setattr(reply, name, deepcopy(getattr(fixture, name)))
                for name in (
                    "selected",
                    "accounted",
                    "http",
                    "ledger",
                    "before_provider",
                    "before_entry",
                    "after_engine",
                    "before_publication",
                ):
                    setattr(reply, name, None)
                for name in (
                    "trace",
                    "transports",
                    "worker_observations",
                    "list_requests",
                    "write_modes",
                    "mutations",
                    "graphql_operations",
                ):
                    setattr(reply, name, [])
                for name in (
                    "old_job_reads",
                    "previous_reads",
                    "completion_thread_reads",
                ):
                    setattr(reply, name, 0)
                reply.terminal_fault_trace_index = None
                reply.past_jobs[reply.run_id] = reply.owner_job
                head = reply.requested_head
                reply.next_run()
                reply.live_head = reply.requested_head = head
                reply.owner_job = "hosted"
                retained_comments = deepcopy(reply.extra_comments)
                source_id = (
                    max(
                        (41, fixture.SOURCE_ID, reply.comment["id"], *retained_comments)
                    )
                    + 1
                )
                self.assertNotIn(source_id, retained_comments)
                reply.prepare_reply(source_id=source_id)
                self.assertEqual(
                    {key: reply.extra_comments[key] for key in retained_comments},
                    retained_comments,
                )
                reply.source["body"] = (
                    "@reviewsensei The caller contract is intentional; the added change is checked."
                )
                reply.reply_document = {
                    "body": "The explanation is supported by the current diff.",
                    "assessments": [
                        {
                            "fingerprint": finding.fingerprint,
                            "decision": "dismissed",
                            "rationale": "The current diff contains the intended added change.",
                            "human_evidence": "The caller contract is intentional",
                            "diff_evidence": "@@ -1 +1,2 @@\n keep\n+change",
                        }
                        for finding in original.human_review.pending
                    ],
                }
                before = reply.provider_calls
                approvals_before = sum(r["state"] == "APPROVED" for r in reply.reviews)
                self.assertEqual(reply.invoke_reply(), 0, reply.error)
                outcome = reply.last_result
                self.assertEqual(outcome.status, "replied")
                self.assertEqual(reply.provider_calls, before + 1)
                self.assertEqual(len(reply.provider_requests), 1)
                current = next(
                    approval_eligibility_from_body(review["body"])
                    for review in reversed(reply.reviews)
                    if approval_eligibility_from_body(review["body"]) is not None
                )
                self.assertFalse(current.human_review.pending)
                self.assertFalse(current.historical_assessments["fresh_human_pending"])
                self.assertEqual(
                    current.historical_assessments["pending"], view["pending"]
                )
                self.assertEqual(current.facts.has_human_adjudication_findings, pending)
                after = parse_session_comment(reply.comment["body"], identity=IDENTITY)
                self.assertEqual(after.historical_assessments, historical)
                self.assertEqual(
                    {key: reply.extra_comments[key] for key in retained_comments},
                    retained_comments,
                )
                if pending:
                    self.assertEqual(
                        sum(r["state"] == "APPROVED" for r in reply.reviews),
                        approvals_before,
                    )
                    self.assertEqual(outcome.approval_status, "approval_withheld")
                else:
                    self.assertEqual(outcome.approval_status, "approved")

    def test_fresh_projection_keeps_both_domains_and_replay_digest(self):
        base = prior()
        resolved = replace(
            base.human_review,
            resolved=tuple(item.fingerprint for item in base.human_review.findings),
        )
        for historical, quarantine in itertools.product((False, True), repeat=2):
            with self.subTest(historical=historical, quarantine=quarantine):
                hold, view = carried_views(True, historical, quarantine)
                original = replace(
                    base, historical_assessments=hold, quarantine_assessments=view
                )
                projected = original.with_fresh_human_review(resolved)
                self.assertFalse(projected.fresh_human_pending)
                self.assertEqual(
                    projected.facts.has_human_adjudication_findings,
                    historical or quarantine,
                )
                self.assertEqual(
                    projected.historical_assessments,
                    {**hold, "fresh_human_pending": False},
                )
                self.assertEqual(
                    projected.quarantine_assessments,
                    {**view, "fresh_human_pending": False},
                )
                self.assertEqual(original.with_fresh_human_review(None), original)
                state = State(original)
                _, prepared = state.bridge()
                self.assertEqual(
                    prepared.replay_context_digest,
                    replace(prepared, eligibility=projected).replay_context_digest,
                )
                invalid = replace(
                    original,
                    historical_assessments={**hold, "fresh_human_pending": False},
                )
                with self.assertRaises(ReviewInputError):
                    invalid.with_fresh_human_review(resolved)

    def test_command_missing_carried_reader_refuses_before_writes(self):
        from test_command_capacity_recovery import CommandCapacityState

        for domain in ("historical_assessments", "quarantine_assessments"):
            with (
                self.subTest(domain=domain),
                tempfile.TemporaryDirectory() as directory,
            ):
                state = CommandCapacityState(
                    complete=True,
                    schema="1",
                    padded=False,
                    human_count=1,
                    inline_range=(0, 1),
                )
                historical, quarantine = carried_views(True, True, True)
                held = replace(
                    state.eligibility,
                    **{
                        domain: historical
                        if domain == "historical_assessments"
                        else quarantine
                    },
                )
                body = state.reviews[0]["body"]
                state.reviews[0]["body"] = body.replace(
                    approval_eligibility_marker(state.eligibility),
                    approval_eligibility_marker(held),
                )
                before = deepcopy(state.reviews)
                with self.assertRaisesRegex(
                    ReviewInputError, "source reader is unavailable"
                ):
                    state.verify(state.record(directory))
                self.assertEqual(state.reviews, before)
                self.assertFalse(
                    any(
                        method in {"POST", "PUT", "PATCH", "DELETE"}
                        for method, _, _ in state.calls
                    )
                )
