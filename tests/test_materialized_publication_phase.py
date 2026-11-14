"""Original source captures only; no smaller price or caller-issued scope."""

import json
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.request import Request

from review_sensei_github import materialized_publication_phase as phase
from review_sensei_github.accepted_result import selected_accepted_result
from review_sensei_github.operation_comments import GitHubIssueCommentOperationPort
from review_sensei_github.quarantine_census import lifecycle_capacity_plan

from review_sensei.errors import ReviewInputError
from tests.test_quarantine_activation import registered_resource
from tests.test_quarantine_journey import JourneyFixture


class MaterializedPublicationPhaseTests(unittest.TestCase):
    def test_installers_cannot_be_reentered_by_a_consumer(self):
        for install, argument in (
            (phase.install_analysis, {}),
            (phase.install_application, object),
            (phase.install_retention, {}),
            (phase.install_census, {}),
        ):
            with (
                self.subTest(install=install.__name__),
                self.assertRaises(ReviewInputError),
            ):
                install(argument)

    def test_actual_plain_source_capture_expires_without_changing_prices(self):
        observed = []
        denied = []
        code = lifecycle_capacity_plan.__code__

        def trace(frame, event, _arg):
            if frame.f_code is code and event == "return":
                found = phase.phase_observation(frame.f_locals["ledger"])
                if found is not None:
                    observed.append(found)
                    if found["closed"] and not denied:
                        ledger = frame.f_locals["ledger"]
                        row = ledger._cached_comments()[0]
                        port = GitHubIssueCommentOperationPort(
                            ledger.http,
                            token=ledger.token,
                            repository=frame.f_locals["identity"].repository,
                            pull_request=frame.f_locals["identity"].pull_request,
                            app_user_id=ledger.authenticated_app_id,
                        )
                        accepted = selected_accepted_result(
                            ledger.http,
                            ledger.caller._effect_record.transaction,
                            app_id=ledger.authenticated_app_id,
                        )
                        accepted_row = ledger._comments_by_id[
                            accepted.reference["event_comment_id"]
                        ]
                        native = Request(
                            ledger.http.api_url + port._list_path,
                            data=json.dumps({"body": accepted_row["body"]}).encode(),
                            method="POST",
                        )
                        before = ledger.caller.accounting.to_document()
                        for send in (
                            lambda: port.create(row["body"]),
                            lambda: port.create("unpriced replacement"),
                            lambda: port.update(row["id"], row["body"]),
                            lambda: port._request(
                                "POST", port._list_path, body={"body": row["body"]}
                            ),
                        ):
                            with self.assertRaisesRegex(ReviewInputError, "forbids"):
                                send()
                            denied.append(True)
                        for name in ("create", "update", "_request"):

                            def foreign(*args, **kwargs):
                                self.fail("substituted writer must not be invoked")

                            with patch.object(
                                GitHubIssueCommentOperationPort, name, foreign
                            ):
                                with self.assertRaisesRegex(
                                    ReviewInputError, "source changed"
                                ):
                                    lifecycle_capacity_plan(
                                        ledger,
                                        frame.f_locals["identity"],
                                        added_parts=0,
                                    )
                        with self.assertRaisesRegex(ReviewInputError, "forbids"):
                            ledger.caller.open(native, timeout=1)
                        port._request = lambda *args, **kwargs: self.fail(
                            "foreign instance executed"
                        )
                        try:
                            with self.assertRaisesRegex(
                                ReviewInputError, "instance changed"
                            ):
                                ledger.caller.open(native, timeout=1)
                        finally:
                            del port._request
                        original_body = accepted_row["body"]
                        accepted_row["body"] += " "
                        try:
                            with self.assertRaises(ReviewInputError):
                                lifecycle_capacity_plan(
                                    ledger, frame.f_locals["identity"], added_parts=0
                                )
                        finally:
                            accepted_row["body"] = original_body
                        self.assertEqual(port.requests, 0)
                        self.assertEqual(ledger.caller.accounting.to_document(), before)
            return trace if frame.f_code is code else None

        with tempfile.TemporaryDirectory() as directory:
            fixture = JourneyFixture(directory)
            _, registration = fixture.original_origins(
                failed_count=1, published_count=1
            )
            prior = sys.gettrace()
            try:
                sys.settrace(trace)
                with registered_resource([registration]):
                    self.assertEqual(fixture.invoke(), 0, fixture.error)
            finally:
                sys.settrace(prior)
            self.assertEqual(len(denied), 4)
            self.assertTrue(observed)
            self.assertTrue(all(item["retained"] for item in observed), observed)
            self.assertTrue(all(item["closed"] for item in observed), observed)
            self.assertTrue(all(not item["poisoned"] for item in observed), observed)
            self.assertTrue(
                any(item["confirmed_read_events"] for item in observed), observed
            )
            self.assertIsNone(phase.phase_observation(fixture.selected.ledger))
            self.assertEqual(
                fixture.selected.ledger.caller.accounting.configuration.limit, 512
            )
