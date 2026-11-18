"""Real stored-lost native writes must reconcile within the original tail."""

import json
import tempfile
import unittest
from urllib.parse import urlsplit

from review_sensei_github.session_ledger import parse_session_comment

from review_sensei.api_work import ApiRunAccounting
from tests import test_recovery_context_g17 as recovery
from tests.test_api_work_persistence import IDENTITY


class OrdinaryPublicationLostNativeTests(unittest.TestCase):
    def test_stored_lost_root_and_approval_open_exact_inline_inventory(self):
        for events in getattr(
            self, "events_options", ({"COMMENT"}, {"APPROVE"}, {"COMMENT", "APPROVE"})
        ):
            with (
                self.subTest(events=events),
                tempfile.TemporaryDirectory() as directory,
            ):
                fixture = recovery.OrdinaryEntryHostedAndCliRecoveryG17Tests._make_hosted_fixture(
                    directory
                )
                original = fixture.external
                lost = set()

                def lose_after_store(request, timeout):
                    response = original(request, timeout)
                    if request.get_method() == "POST" and urlsplit(
                        request.full_url
                    ).path.endswith("/pulls/7/reviews"):
                        event = json.loads(request.data)["event"]
                        if event in events and event not in lost:
                            lost.add(event)
                            raise TimeoutError("synthetic stored native response loss")
                    return response

                fixture.external = lose_after_store
                code = fixture.invoke()
                self.assertEqual(code, 0, fixture.error)
                self.assertEqual(lost, events)
                self.assertEqual(
                    [review["state"] for review in fixture.reviews],
                    ["COMMENTED", "APPROVED"],
                )
                opened = [
                    path
                    for _, method, path in fixture.trace
                    if method == "GET"
                    and "/reviews/" in path
                    and path.endswith("/comments")
                ]
                self.assertGreaterEqual(len(opened), len(events))
                current = parse_session_comment(
                    fixture.comment["body"], identity=IDENTITY
                )
                self.assertIsNotNone(
                    ApiRunAccounting.from_document(
                        current.api_run_accounting
                    ).terminal_transaction
                )
                self.assertEqual(fixture.accounted.accounting.configuration.limit, 512)

    def test_stored_lost_check_intents_keep_one_check_and_terminal_receipt(self):
        for operations in ({"POST"}, {"PATCH"}, {"POST", "PATCH"}):
            with (
                self.subTest(operations=operations),
                tempfile.TemporaryDirectory() as directory,
            ):
                fixture = recovery.OrdinaryEntryHostedAndCliRecoveryG17Tests._make_hosted_fixture(
                    directory
                )
                original = fixture.external
                lost = set()

                def lose_after_store(request, timeout):
                    response = original(request, timeout)
                    method = request.get_method()
                    path = urlsplit(request.full_url).path
                    if (
                        method in operations
                        and method not in lost
                        and "/check-runs" in path
                    ):
                        lost.add(method)
                        raise TimeoutError("synthetic stored check response loss")
                    return response

                fixture.external = lose_after_store
                self.assertEqual(fixture.invoke(), 0, fixture.error)
                self.assertEqual(lost, operations)
                self.assertEqual(len(fixture.check_runs), 1)
                self.assertEqual(fixture.check_runs[0]["status"], "completed")
                self.assertEqual(
                    [review["state"] for review in fixture.reviews],
                    ["COMMENTED", "APPROVED"],
                )
                current = parse_session_comment(
                    fixture.comment["body"], identity=IDENTITY
                )
                self.assertIsNotNone(
                    ApiRunAccounting.from_document(
                        current.api_run_accounting
                    ).terminal_transaction
                )

    def test_all_four_native_responses_lost_stay_within_original_allowance(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = (
                recovery.OrdinaryEntryHostedAndCliRecoveryG17Tests._make_hosted_fixture(
                    directory
                )
            )
            original = fixture.external
            lost = set()

            def lose(request, timeout):
                response = original(request, timeout)
                method, path = request.get_method(), urlsplit(request.full_url).path
                key = None
                if method in {"POST", "PATCH"} and "/check-runs" in path:
                    key = method
                elif method == "POST" and path.endswith("/pulls/7/reviews"):
                    key = json.loads(request.data)["event"]
                if key is not None and key not in lost:
                    lost.add(key)
                    raise TimeoutError("synthetic stored response loss")
                return response

            fixture.external = lose
            self.assertEqual(fixture.invoke(), 0, fixture.error)
            self.assertEqual(lost, {"POST", "PATCH", "COMMENT", "APPROVE"})
            self.assertEqual(len(fixture.check_runs), 1)
            self.assertEqual(
                [r["state"] for r in fixture.reviews], ["COMMENTED", "APPROVED"]
            )
            self.assertEqual(fixture.accounted.accounting.configuration.limit, 512)
            self.assertIsNotNone(fixture.accounted.accounting.terminal_transaction)
            unknown = [
                o
                for o in fixture.accounted.observations
                if o.get("mutation") and o["outcome"] == "unknown"
            ]
            self.assertEqual(len(unknown), 4)
            self.assertTrue(
                all(
                    o["request_id"] in fixture.accounted._reconciled_mutations
                    for o in unknown
                )
            )
            self.assertGreaterEqual(fixture.accounted.accounting.uncertain, 4)
