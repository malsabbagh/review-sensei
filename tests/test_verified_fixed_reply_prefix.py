"""Real reassessment produces verified fixes; uncertainty never grants a new run."""

import copy
import json
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from review_sensei_github.finding_lifecycle import FindingLifecycleJournal
from review_sensei_github.publication import approval_eligibility_from_body

from review_sensei.finding_lifecycle import decode_lifecycle_state
from tests.fake_github_http import json_response
from tests.test_action_reply_entry import ReplyEntryFixture
from tests.test_api_work_persistence import HEAD
from tests.test_review_run import EntryFixture


def obligations(document):
    state = decode_lifecycle_state(document["lifecycle"]).state
    if hasattr(state, "inventory"):
        return {
            item.obligation_id: (
                item.lifecycle_state,
                item.thread_binding.slot_state if item.thread_binding else None,
                item.resolution_proof_sha256,
                item.criterion,
            )
            for item in state.inventory.obligations
        }
    return {
        item.obligation_id: (
            item.lifecycle_state,
            item.target_state,
            item.resolution_proof_sha256,
            item.resolution_criterion,
        )
        for item in state.authoritative_decision_inventory.obligations
    }


class VerifiedFixedReplyPrefixTests(unittest.TestCase):
    def fixture(self, directory, *, limit=512):
        prior = EntryFixture(directory, limit=limit)
        prior.provider_comments = [
            dict(
                path="src/app.py",
                line=2,
                side="RIGHT",
                body=f"Confirm assumption {index}",
                severity="high",
                blocking=False,
                needs_human=True,
            )
            for index in range(2)
        ]
        self.assertEqual(prior.invoke(), 0, prior.error)
        eligibility = approval_eligibility_from_body(prior.reviews[-1]["body"])
        fixture = ReplyEntryFixture.__new__(ReplyEntryFixture)
        fixture.__dict__.update(prior.__dict__)
        fixture.next_run()
        fixture.live_head = fixture.requested_head = HEAD
        fixture.prepare_reply()
        fixture.thread_rows = copy.deepcopy(fixture.threads)
        for node in fixture.thread_rows:
            for comment in node["comments"]["nodes"]:
                comment["author"].pop(
                    "databaseId", None
                )  # Actual discovery query shape.
        fixture.source["body"] = (
            "@reviewsensei The caller contract is intentional; the added change is checked."
        )
        fixture.reply_document = {
            "body": "The explanation is supported by the current diff.",
            "assessments": [
                dict(
                    fingerprint=item.fingerprint,
                    decision="dismissed",
                    rationale="The current diff contains the intended added change.",
                    human_evidence="The caller contract is intentional",
                    diff_evidence="@@ -1 +1,2 @@\n keep\n+change",
                )
                for item in eligibility.human_review.pending
            ],
        }
        return fixture

    def test_real_two_finding_reassessment_preserves_criteria_and_releases_confirmed_prefix(
        self,
    ):
        for fail_at in (None, 0, 1):
            with (
                self.subTest(fail_at=fail_at),
                tempfile.TemporaryDirectory() as directory,
            ):
                documents = []
                checkpoint = FindingLifecycleJournal.checkpoint

                def capture(journal, **kwargs):
                    result = checkpoint(journal, **kwargs)
                    documents.append(copy.deepcopy(journal._document()))
                    return result

                with patch.object(FindingLifecycleJournal, "checkpoint", capture):
                    fixture = self.fixture(directory)
                    original = obligations(documents[-1])
                    attempts = []
                    external = fixture.external

                    def reject(request, timeout):
                        body = json.loads(request.data) if request.data else {}
                        if (
                            urlsplit(request.full_url).path == "/graphql"
                            and body.get("operationName") == "ResolveReviewThread"
                        ):
                            attempts.append(body["variables"]["input"]["threadId"])
                            if len(attempts) - 1 == fail_at:
                                fixture.trace.append(
                                    (fixture.attempt, request.get_method(), "/graphql")
                                )
                                return json_response(
                                    {"errors": [{"type": "FORBIDDEN"}]}, 403
                                )
                        return external(request, timeout)

                    fixture.external = reject
                    before_checks = copy.deepcopy(fixture.check_runs)
                    before_review_count = len(fixture.reviews)
                    code = fixture.invoke_reply()
                current = obligations(documents[-1])
                self.assertEqual(set(current), set(original))
                self.assertTrue(
                    all(current[key][3] == original[key][3] for key in current)
                )
                self.assertTrue(
                    all(
                        value[0] == "verified-fixed" and value[2]
                        for value in current.values()
                    )
                )
                self.assertEqual(len(attempts), 2 if fail_at is None else fail_at + 1)
                self.assertEqual(
                    sum(value[1] == "released" for value in current.values()),
                    2 if fail_at is None else fail_at,
                    documents[-1]["effects"],
                )
                self.assertEqual(fixture.limit, 512)
                if fail_at is None:
                    self.assertEqual(code, 0, fixture.error)
                    self.assertEqual(fixture.action()["phase"], "terminal")
                else:
                    self.assertEqual(code, 1, fixture.error)
                    self.assertEqual(fixture.action()["phase"], "pending")
                    self.assertEqual(
                        fixture.last_result.approval_status, "gate_reevaluation_failed"
                    )
                    self.assertIsNone(fixture.last_result.finalization_completion)
                    self.assertEqual(fixture.check_runs, before_checks)
                    self.assertFalse(
                        any(
                            review["state"] == "APPROVED"
                            for review in fixture.reviews[before_review_count:]
                        )
                    )
                    before = (
                        fixture.provider_calls,
                        fixture.reply_posts,
                        len(attempts),
                    )
                    fixture.attempt += 1
                    self.assertEqual(fixture.invoke_reply(), 1)
                    self.assertEqual(
                        (fixture.provider_calls, fixture.reply_posts, len(attempts)),
                        before,
                    )

    def test_actual_one_short_tail_refuses_before_reply_or_native_business(self):
        from review_sensei_github.api_run_budget import CallerRunBudget

        observed = []
        original = CallerRunBudget.preflight

        def capture(budget, calls):
            if calls > budget.ordinary_remaining():
                observed.append((calls, budget.ordinary_remaining()))
            return original(budget, calls)

        with tempfile.TemporaryDirectory() as directory:
            fixture = self.fixture(directory, limit=501)
            before = (
                copy.deepcopy(fixture.reviews),
                copy.deepcopy(fixture.check_runs),
                copy.deepcopy(fixture.thread_rows),
            )
            with patch.object(CallerRunBudget, "preflight", capture):
                self.assertEqual(fixture.invoke_reply(), 1)
            self.assertEqual(observed, [(104, 103)], fixture.error)
            self.assertEqual(fixture.reply_posts, 0)
            self.assertEqual(
                (fixture.reviews, fixture.check_runs, fixture.thread_rows), before
            )
            self.assertEqual(fixture.limit, 501)

    def test_lost_first_cleanup_response_retains_unknown_and_stops_suffix(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.fixture(directory)
            external = fixture.external
            attempts = []
            unknown = False

            def lose(request, timeout):
                nonlocal unknown
                body = json.loads(request.data) if request.data else {}
                if urlsplit(request.full_url).path == "/graphql":
                    if body.get("operationName") == "ResolveReviewThread":
                        attempts.append(body["variables"]["input"]["threadId"])
                        external(request, timeout)  # Actual native mutation landed.
                        unknown = True
                        raise TimeoutError("native mutation response lost")
                    if unknown and body.get("operationName") == "ResolutionReadback":
                        fixture.trace.append(
                            (fixture.attempt, request.get_method(), "/graphql")
                        )
                        return json_response({"errors": [{"type": "UNAVAILABLE"}]}, 503)
                return external(request, timeout)

            fixture.external = lose
            self.assertEqual(fixture.invoke_reply(), 1)
            self.assertEqual(len(attempts), 1)
            self.assertEqual(len(fixture.resolved_threads), 1)
            self.assertEqual(fixture.action()["phase"], "pending")
            before = (fixture.provider_calls, fixture.reply_posts, len(attempts))
            fixture.attempt += 1
            self.assertEqual(fixture.invoke_reply(), 1)
            self.assertEqual(
                (fixture.provider_calls, fixture.reply_posts, len(attempts)), before
            )

    def test_copied_mutated_or_foreign_capacity_custody_cannot_publish(self):
        from review_sensei_github.reply_finalization_plan import execute_reply_capacity

        for fault in ("clone", "reference", "root", "ledger", "consumer"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                fixture = self.fixture(directory)
                before = (
                    copy.deepcopy(fixture.reviews),
                    copy.deepcopy(fixture.check_runs),
                )

                def altered(plan, publisher):
                    if fault == "clone":
                        plan = copy.copy(plan)
                    elif fault == "reference":
                        plan.record.finding_lifecycle["operation_id"] = (
                            "foreign-operation"
                        )
                    elif fault == "root":
                        plan.journal.ledger.caller._root = "0" * 64
                    else:
                        publisher.finalizer._finding_lifecycle_consumer.ledger = (
                            object()
                        )
                    return execute_reply_capacity(plan, publisher)

                with patch(
                    "review_sensei_github.reply_finalization_plan.execute_reply_capacity",
                    altered,
                ):
                    self.assertEqual(fixture.invoke_reply(), 1)
                self.assertEqual(fixture.reply_posts, 0)
                self.assertEqual((fixture.reviews, fixture.check_runs), before)

    def test_native_criterion_or_owner_edit_holds_before_visible_reply(self):
        for fault in ("criterion", "owner"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                fixture = self.fixture(directory)
                root = fixture.thread_rows[0]["comments"]["nodes"][0]
                if fault == "criterion":
                    root["body"] += "\nChanged native criterion."
                else:
                    root["author"]["login"] = "foreign-owner"
                before = (
                    copy.deepcopy(fixture.reviews),
                    copy.deepcopy(fixture.check_runs),
                )
                self.assertEqual(fixture.invoke_reply(), 1)
                self.assertEqual(fixture.reply_posts, 0)
                self.assertEqual(fixture.resolved_threads, [])
                self.assertEqual((fixture.reviews, fixture.check_runs), before)

    def test_preconfirmed_group_and_unexpected_resolved_suffix_are_not_conflated(self):
        checkpoint = FindingLifecycleJournal.checkpoint
        for late in (False, True):
            with self.subTest(late=late), tempfile.TemporaryDirectory() as directory:
                fixture = self.fixture(directory)
                documents = []
                external = fixture.external
                if not late:
                    fixture.thread_rows[-1]["isResolved"] = True

                def observe(request, timeout):
                    body = json.loads(request.data) if request.data else {}
                    if (
                        late
                        and request.get_method() == "POST"
                        and body.get("event") == "COMMENT"
                    ):
                        fixture.thread_rows[-1]["isResolved"] = True
                    return external(request, timeout)

                def capture(journal, **kwargs):
                    result = checkpoint(journal, **kwargs)
                    documents.append(copy.deepcopy(journal._document()))
                    return result

                fixture.external = observe
                with patch.object(FindingLifecycleJournal, "checkpoint", capture):
                    self.assertEqual(fixture.invoke_reply(), 0, fixture.error)
                self.assertEqual(len(fixture.resolved_threads), 1)
                current = obligations(documents[-1])
                self.assertEqual(
                    sum(v[1] == "released" for v in current.values()), 1 if late else 2
                )
                self.assertEqual(len(current), 2)

    def test_instance_shadowed_finalizer_is_rejected_without_invoking_it(self):
        from review_sensei_github.reply_finalization_plan import prepare_reply_capacity

        calls = []

        def inject(publisher, **kwargs):
            def extra_reads(**arguments):
                calls.append(arguments)
                raise AssertionError("unpriced callback must not execute")

            publisher.finalizer.finalize = extra_reads
            return prepare_reply_capacity(publisher, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            fixture = self.fixture(directory)
            with patch(
                "review_sensei_github.reply_finalization_plan.prepare_reply_capacity",
                inject,
            ):
                self.assertEqual(fixture.invoke_reply(), 1)
            self.assertEqual(calls, [])
            self.assertEqual(fixture.reply_posts, 0)
            self.assertEqual(fixture.resolved_threads, [])

    def test_nonapproving_mixed_projection_is_unqualified_without_blocking_human_only_reply(
        self,
    ):
        import subprocess
        from dataclasses import replace

        from review_sensei_github.publication import parse_finding_index
        from review_sensei_github.reply_finalization_plan import (
            require_closed_reply_check_tail,
        )

        from review_sensei.errors import ReviewInputError

        with tempfile.TemporaryDirectory() as directory:
            fixture = self.fixture(directory)
            original = approval_eligibility_from_body(fixture.reviews[-1]["body"])
            index = parse_finding_index(
                fixture.reviews[-1]["body"],
                repository_id=123,
                pull_request=7,
                head_sha=HEAD,
            )
            # Negative-only capacity projection, never installed as native or
            # selected lifecycle authority and never used to issue a plan.
            mixed = replace(
                index,
                entries=(
                    *index.entries,
                    replace(
                        index.entries[0],
                        instance="0" * 64,
                        blocking=True,
                        needs_human=False,
                    ),
                ),
            )
            with self.assertRaisesRegex(
                ReviewInputError, "reply_capacity_inline_blocking_tail_unqualified"
            ):
                require_closed_reply_check_tail("advisory", original, mixed)
            fixture.config_bytes = fixture.config_bytes.replace(
                b"github:\n", b"github:\n  reviews: advisory\n"
            )
            (fixture.root / ".reviewsensei.yml").write_bytes(fixture.config_bytes)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(fixture.root),
                    "-c",
                    "user.name=Fixture",
                    "-c",
                    "user.email=fixture@example.invalid",
                    "commit",
                    "-am",
                    "advisory reply policy",
                ],
                check=True,
                capture_output=True,
            )
            fixture.policy = subprocess.check_output(
                ["git", "-C", str(fixture.root), "rev-parse", "HEAD"], text=True
            ).strip()
            fixture.policy_ref = fixture.policy
            self.assertEqual(fixture.invoke_reply(), 0, fixture.error)
            self.assertEqual(
                fixture.last_result.approval_status, "auto_approval_disabled"
            )

    def test_custody_change_after_zero_restore_quote_has_no_unpriced_fallback(self):
        from review_sensei_github.reply_finalization_plan import ReplyFinalizationPlan

        bind = ReplyFinalizationPlan.bind_capacity

        def changed(plan, **kwargs):
            caller = plan.journal.ledger.caller
            caller._effect_record = copy.deepcopy(caller._effect_record)
            return bind(plan, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            fixture = self.fixture(directory)
            before = (copy.deepcopy(fixture.reviews), copy.deepcopy(fixture.check_runs))
            with patch.object(ReplyFinalizationPlan, "bind_capacity", changed):
                self.assertEqual(fixture.invoke_reply(), 1)
            self.assertEqual(fixture.reply_posts, 0)
            self.assertEqual((fixture.reviews, fixture.check_runs), before)
            self.assertIn("reply retained custody changed after pricing", fixture.error)
