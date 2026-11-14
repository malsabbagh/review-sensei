"""Cold commands consume genuine terminal reply proofs without inventing authority."""

import copy
import dis
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from urllib.parse import urlsplit

from review_sensei_github.finding_lifecycle import FindingLifecycleJournal
from review_sensei_github.publication import approval_eligibility_from_body
from review_sensei_github.session_ledger import parse_session_comment

from tests import test_verified_fixed_reply_prefix as reply_fixtures
from tests.fake_github_http import json_response
from tests.test_action_run import IDENTITY, CommandEntryFixture
from tests.test_verified_fixed_reply_prefix import obligations


class VerifiedFixedCommandPrefixTests(unittest.TestCase):
    def fixture(self, directory, *, reopened):
        reply = reply_fixtures.VerifiedFixedReplyPrefixTests().fixture(directory)
        documents = []
        checkpoint = FindingLifecycleJournal.checkpoint
        external = reply.external

        @contextmanager
        def capture(extra=None, extra_code=None):
            # Profiling leaves coverage tracing installed. Only genuine Python
            # return instructions count; exception unwind is not a receipt.
            returns = {
                code: {
                    item.offset
                    for item in dis.get_instructions(code)
                    if item.opname in {"RETURN_VALUE", "RETURN_CONST"}
                }
                for code in (checkpoint.__code__, extra_code)
                if code is not None
            }

            def observe(frame, event, arg):
                successful_return = event == "return" and frame.f_lasti in returns.get(
                    frame.f_code, ()
                )
                if frame.f_code is checkpoint.__code__ and successful_return:
                    documents.append(copy.deepcopy(frame.f_locals["self"]._document()))
                if (
                    extra is not None
                    and frame.f_code is extra_code
                    and (event == "call" or successful_return)
                ):
                    extra(frame, event, arg)

            previous = sys.getprofile()
            try:
                sys.setprofile(observe)
                yield
            finally:
                sys.setprofile(previous)

        def externally_resolve(request, timeout):
            body = json.loads(request.data) if request.data else {}
            if request.get_method() == "POST" and body.get("event") == "COMMENT":
                # Native users resolve after the reply plan's observation. The
                # successful reply must not release this unpriced subset.
                for node in reply.thread_rows:
                    node["isResolved"] = True
            return external(request, timeout)

        reply.external = externally_resolve
        with capture():
            self.assertEqual(reply.invoke_reply(), 0, reply.error)
        self.assertEqual(reply.action()["phase"], "terminal")
        original = obligations(documents[-1])
        self.assertEqual(len(original), 2)
        self.assertTrue(
            all(
                v[0] == "verified-fixed" and v[1] == "contingent" and v[2]
                for v in original.values()
            )
        )
        self.assertEqual(reply.resolved_threads, [])
        command = CommandEntryFixture.__new__(CommandEntryFixture)
        command.__dict__.update(reply.__dict__)
        command.__dict__.pop("external", None)
        command.command_active = False
        command.command_body = "@reviewsensei verify"
        command.source = None
        command.source_reads = 0
        command.before_source_read = None
        command.before_business_write = None
        command.grant_fault = None
        command.action_fault = None
        command.business_writes = command.closure_writes = 0
        command.broker_requests = []
        command.review_updates = command.review_put_attempts = 0
        command.put_fault = None
        command.resolved_threads = set()
        command.past_jobs = {}
        command.stdout = command.error = ""
        command.exception = None
        command.threads = copy.deepcopy(reply.thread_rows)
        if reopened:
            for node in command.threads:
                node["isResolved"] = False
        command.select_command("@reviewsensei verify", new_run=True)
        return command, original, documents, capture

    def action(self, fixture):
        return parse_session_comment(
            fixture.comment["body"], identity=IDENTITY
        ).api_run_accounting["action"]

    def assert_preserved(self, original, current):
        self.assertEqual(set(current), set(original))
        for key, value in current.items():
            self.assertEqual(value[0], "verified-fixed")
            self.assertEqual(value[2:], original[key][2:])

    def test_original_512_reopened_and_preconfirmed_targets_then_cold_verify(self):
        for reopened in (True, False):
            with (
                self.subTest(reopened=reopened),
                tempfile.TemporaryDirectory() as directory,
            ):
                fixture, original, documents, capture = self.fixture(
                    directory, reopened=reopened
                )
                with capture():
                    self.assertEqual(
                        fixture.invoke_command(), 0, fixture.error + fixture.stdout
                    )
                current = obligations(documents[-1])
                self.assert_preserved(original, current)
                self.assertEqual([v[1] for v in current.values()], ["released"] * 2)
                self.assertEqual(len(fixture.resolved_threads), 2 if reopened else 0)
                self.assertEqual(self.action(fixture)["phase"], "terminal")
                fixture.SOURCE_ID += 1
                fixture.select_command("@reviewsensei verify", new_run=True)
                with capture():
                    self.assertEqual(
                        fixture.invoke_command(), 0, fixture.error + fixture.stdout
                    )
                self.assert_preserved(original, obligations(documents[-1]))
                self.assertEqual(self.action(fixture)["phase"], "terminal")
                self.assertEqual(fixture.limit, 512)

    def test_failed_cleanup_preserves_prefix_and_cold_uncertainty(self):
        for failure in (0, 1, "lost", "mixed"):
            with (
                self.subTest(failure=failure),
                tempfile.TemporaryDirectory() as directory,
            ):
                fixture, original, documents, capture = self.fixture(
                    directory, reopened=True
                )
                if failure == "mixed":
                    fixture.threads[0]["isResolved"] = True
                external = fixture.external
                attempts = []
                unknown = False

                def fail(request, timeout):
                    nonlocal unknown
                    body = json.loads(request.data) if request.data else {}
                    if urlsplit(request.full_url).path == "/graphql":
                        if body.get("operationName") == "ResolveReviewThread":
                            attempts.append(body["variables"]["input"]["threadId"])
                            if failure == "lost":
                                external(request, timeout)
                                unknown = True
                                raise TimeoutError("native response lost")
                            if len(attempts) - 1 == failure or failure == "mixed":
                                fixture._meter(request)
                                return json_response(
                                    {"errors": [{"type": "FORBIDDEN"}]}, 403
                                )
                        if (
                            unknown
                            and body.get("operationName") == "ResolutionReadback"
                        ):
                            fixture._meter(request)
                            return json_response(
                                {"errors": [{"type": "UNAVAILABLE"}]}, 503
                            )
                    return external(request, timeout)

                fixture.external = fail
                checks = copy.deepcopy(fixture.check_runs)
                review_count = len(fixture.reviews)
                with capture():
                    self.assertEqual(
                        fixture.invoke_command(), 1, fixture.error + fixture.stdout
                    )
                current = obligations(documents[-1])
                self.assert_preserved(original, current)
                expected_prefix = (
                    0 if failure == "lost" else 1 if failure == "mixed" else failure
                )
                self.assertEqual(
                    sum(v[1] == "released" for v in current.values()), expected_prefix
                )
                self.assertEqual(
                    len(attempts), 1 if failure == "mixed" else expected_prefix + 1
                )
                self.assertEqual(self.action(fixture)["phase"], "pending")
                self.assertEqual(fixture.check_runs, checks)
                self.assertFalse(
                    any(
                        r["state"] == "APPROVED" for r in fixture.reviews[review_count:]
                    )
                )
                before = (
                    len(attempts),
                    fixture.review_put_attempts,
                    copy.deepcopy(fixture.check_runs),
                )
                fixture.attempt += 1
                self.assertEqual(fixture.invoke_command(), 1)
                self.assertEqual(
                    (len(attempts), fixture.review_put_attempts, fixture.check_runs),
                    before,
                )

    def test_invalid_two_frame_frontier_refuses_before_business(self):
        for fault in ("owner", "duplicate_index", "duplicate_authority", "result"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                fixture, _, _, _ = self.fixture(directory, reopened=True)
                authority = next(
                    r
                    for r in fixture.reviews
                    if "reviewsensei:human-review:v1" in r["body"]
                )
                if fault == "owner":
                    authority["user"] = {
                        "id": 888,
                        "login": "foreign[bot]",
                        "type": "Bot",
                    }
                elif fault == "duplicate_authority":
                    duplicate = copy.deepcopy(authority)
                    duplicate["id"] = max(r["id"] for r in fixture.reviews) + 100
                    fixture.reviews.append(duplicate)
                elif fault == "duplicate_index":
                    duplicate = copy.deepcopy(fixture.reviews[0])
                    duplicate["id"] = max(r["id"] for r in fixture.reviews) + 100
                    fixture.reviews.append(duplicate)
                else:
                    eligibility = approval_eligibility_from_body(authority["body"])
                    authority["body"] = authority["body"].replace(
                        eligibility.result_digest, "f" * 64
                    )
                before = (
                    copy.deepcopy(fixture.reviews),
                    copy.deepcopy(fixture.check_runs),
                    copy.deepcopy(fixture.threads),
                )
                fixture.invoke_command()
                self.assertEqual(
                    (fixture.reviews, fixture.check_runs, fixture.threads), before
                )
                self.assertEqual(fixture.review_put_attempts, 0)
                self.assertEqual(fixture.resolved_threads, set())

    def test_empty_cleanup_retains_check_plan_and_original_source_guard(self):
        from review_sensei_github.command_finalization_plan import (
            _CLEANUP,
            cleanup_capacity_states,
        )

        from review_sensei.bounded_evidence import EvidenceReadBudget
        from review_sensei.errors import ReviewInputError
        from tests.test_command_finalization_plan import CommandFinalizationPlanTests
        from tests.test_plain_cleanup_fence import PlainCleanupFenceTests

        state, finalizer, args, calls = PlainCleanupFenceTests().fixture()
        finalizer.evidence_budget = EvidenceReadBudget()
        original = copy.deepcopy(state.reviews)
        plan = CommandFinalizationPlanTests().prepare(finalizer, args)
        self.assertEqual(json.loads(plan.payload)["cleanup_requests"], [])
        self.assertIsNotNone(json.loads(plan.payload)["check_request"])
        self.assertNotIn(plan, _CLEANUP)
        self.assertEqual(cleanup_capacity_states(plan), plan.states)
        self.assertEqual(state.reviews, original)
        self.assertTrue(all(method == "GET" for method, _ in calls))
        with self.assertRaises(ReviewInputError):
            copy.copy(plan).require(plan.journal, http=plan.http, budget=plan.budget)

    def test_actual_one_short_new_command_preserves_original_producer_history(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, original, _, _ = self.fixture(directory, reopened=True)
            prior = parse_session_comment(fixture.comment["body"], identity=IDENTITY)
            self.assertEqual(fixture.limit, 512)
            fixture.config_bytes = fixture.config_bytes.replace(
                b"api_request_limit: 512", b"api_request_limit: 457"
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
                    "smaller command allowance",
                ],
                check=True,
                capture_output=True,
            )
            fixture.policy = fixture.policy_ref = subprocess.check_output(
                ["git", "-C", str(fixture.root), "rev-parse", "HEAD"], text=True
            ).strip()
            fixture.limit = 457
            before = copy.deepcopy(
                (fixture.reviews, fixture.check_runs, fixture.threads)
            )
            refused = []

            def observe(frame, event, arg):
                if (
                    event == "call"
                    and frame.f_code.co_name == "preflight"
                    and frame.f_code.co_filename.endswith("api_run_budget.py")
                ):
                    budget = frame.f_locals["self"]
                    calls = frame.f_locals["calls"]
                    if (
                        budget.caller._state is not None
                        and calls > budget.ordinary_remaining()
                    ):
                        refused.append((calls, budget.ordinary_remaining()))
                return None

            previous = sys.gettrace()
            sys.settrace(observe)
            try:
                self.assertEqual(fixture.invoke_command(), 0, fixture.error)
            finally:
                sys.settrace(previous)
            self.assertEqual(refused, [(85, 84)])
            self.assertEqual(
                (fixture.reviews, fixture.check_runs, fixture.threads), before
            )
            self.assertEqual(fixture.review_put_attempts, 0)
            self.assertEqual(fixture.resolved_threads, set())
            self.assertEqual(self.action(fixture)["phase"], "terminal")
            self.assertEqual(self.action(fixture)["outcome"]["kind"], "explicit_noop")
            # The prior terminal source remains immutable; the reduced policy
            # applies to the new command, never retroactively erasing liabilities.
            self.assertEqual(prior.api_run_accounting["configuration"]["limit"], 512)

    def test_mixed_preconfirmed_and_changed_execution_sources(self):
        from review_sensei_github import command_finalization_plan as plans

        for fault in (
            None,
            "clone",
            "frontier",
            "criterion",
            "preconfirmed_criterion",
            "late_preconfirmed_criterion",
        ):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                fixture, original, documents, capture = self.fixture(
                    directory, reopened=True
                )
                fixture.threads[0]["isResolved"] = True
                if fault is None:
                    # A real deleted check forces a new check POST after mixed
                    # cleanup, exposing its exact pending-state commitment.
                    fixture.check_runs.clear()
                if fault == "preconfirmed_criterion":
                    fixture.threads[0]["comments"]["nodes"][0]["body"] += (
                        "\nEdited concern."
                    )
                if fault == "criterion":
                    fixture.threads[1]["comments"]["nodes"][0]["body"] += (
                        "\nEdited concern."
                    )
                before = copy.deepcopy((fixture.reviews, fixture.check_runs))
                attach = plans.attach_execution_reads

                def transfer(frame, event, arg):
                    if frame.f_code is not attach.__code__:
                        return
                    if event == "call" and fault == "clone":
                        values = frame.f_locals
                        # Exercise the genuine entry with a copied capability;
                        # the refused call must not dispatch any native request.
                        attach(
                            copy.copy(values["plan"]),
                            values["finalizer"],
                            values["journal"],
                            dispositions=values["dispositions"],
                            check_token=values["check_token"],
                        )
                        self.fail("copied execution plan was accepted")
                    if event != "return":
                        return
                    if fault == "late_preconfirmed_criterion":
                        fixture.threads[0]["comments"]["nodes"][0]["body"] += (
                            "\nEdited after planning."
                        )
                    if fault == "frontier":
                        authority = next(
                            r
                            for r in fixture.reviews
                            if "reviewsensei:human-review:v1" in r["body"]
                        )
                        authority["body"] += "\nChanged after planning."

                with capture(transfer, attach.__code__):
                    code = fixture.invoke_command()
                if fault is None:
                    self.assertEqual(code, 0, fixture.error)
                    self.assertEqual(len(fixture.resolved_threads), 1)
                    current = obligations(documents[-1])
                    self.assert_preserved(original, current)
                    self.assertEqual([v[1] for v in current.values()], ["released"] * 2)
                else:
                    self.assertEqual(fixture.resolved_threads, set())
                    self.assertEqual(fixture.review_put_attempts, 0)
                    self.assertEqual(fixture.check_runs, before[1])
                    if fault != "frontier":
                        self.assertEqual(fixture.reviews, before[0])


if __name__ == "__main__":
    unittest.main()
