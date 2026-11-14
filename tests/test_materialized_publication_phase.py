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
            (phase.install_publisher, object),
        ):
            with (
                self.subTest(install=install.__name__),
                self.assertRaises(ReviewInputError),
            ):
                install(argument)

    def test_actual_plain_source_capture_expires_without_changing_prices(self):
        observed = []
        denied = []
        denied_replays = []
        wrapped_attributes = []
        code = lifecycle_capacity_plan.__code__

        def trace(frame, event, _arg):
            own_write = (
                frame.f_code.co_name == "require_journal_write"
                and frame.f_globals is vars(phase)
            )
            if own_write and event == "return" and not denied_replays:
                entry = frame.f_locals.get("entry")
                source = frame.f_locals.get("source")
                if entry is not None and source in entry["write_frames"]:
                    caller = entry["caller"]
                    before = caller.accounting.to_document()
                    with self.assertRaisesRegex(
                        ReviewInputError, "outside original journal"
                    ):
                        caller.open(frame.f_locals["request"], timeout=1)
                    self.assertEqual(caller.accounting.to_document(), before)
                    journal = entry["journal"]
                    record = journal._record
                    try:
                        journal._record = None
                        with self.assertRaisesRegex(ReviewInputError, "physical root"):
                            caller.open(frame.f_locals["request"], timeout=1)
                    finally:
                        journal._record = record
                    self.assertEqual(caller.accounting.to_document(), before)
                    denied_replays.append(True)
            if frame.f_code is code and event == "return":
                found = phase.phase_observation(frame.f_locals["ledger"])
                if found is not None:
                    observed.append(found)
                    if found["closed"] and not denied:
                        ledger = frame.f_locals["ledger"]
                        from review_sensei_github.finding_lifecycle import (
                            FindingLifecycleJournal,
                        )

                        checkpoint = FindingLifecycleJournal.checkpoint
                        # A later defining-source wrapper may surround the
                        # checkpoint; mutate only the captured original's
                        # otherwise unused convenience attribute.
                        while hasattr(checkpoint, "__wrapped__"):
                            checkpoint = checkpoint.__wrapped__
                        self.assertFalse(hasattr(checkpoint, "__wrapped__"))
                        checkpoint.__wrapped__ = object()
                        wrapped_attributes.append(checkpoint)
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
                        # Arbitrary archive/event/evidence bodies are not an
                        # executable current-invocation allocation source.
                        for body in (
                            "new historical decision archive",
                            "ReviewSensei immutable evidence part v1\nforged",
                        ):
                            foreign_request = Request(
                                ledger.http.api_url + port._list_path,
                                data=json.dumps({"body": body}).encode(),
                                method="POST",
                            )
                            with self.assertRaisesRegex(
                                ReviewInputError, "materialized publication"
                            ):
                                ledger.caller.open(foreign_request, timeout=1)
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
            return trace if frame.f_code is code or own_write else None

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
                for function in wrapped_attributes:
                    del function.__wrapped__
            self.assertEqual(len(denied), 4)
            self.assertEqual(denied_replays, [True])
            self.assertTrue(observed)
            captures = [item for item in observed if item["censuses"] == 1]
            self.assertTrue(captures)
            for item in captures:
                self.assertIsNone(item["projection_error"], item)
                self.assertIsNotNone(item["projection"], item)
                self.assertFalse(item["projection"]["authority"])
                self.assertFalse(item["projection"]["complete_funding_proof"])
                self.assertFalse(item["projection"]["invocation_price_issued"])
                self.assertEqual(item["projection"]["protected_terminal_remaining"], 50)
                self.assertTrue(item["projection"]["branches"])
                self.assertEqual(
                    {
                        branch["check_published_frame"]
                        for branch in item["projection"]["branches"]
                    },
                    {False, True},
                )
                for branch in item["projection"]["branches"]:
                    self.assertEqual(
                        len(branch["pending_document_bytes"]), branch["effects"]
                    )
                    self.assertEqual(
                        len(branch["stop_document_bytes"]), branch["effects"]
                    )
                    self.assertGreater(branch["maximum_finish_document_bytes"], 0)
                    sequences = branch["pending_and_stop_sequences"]
                    self.assertEqual(len(sequences), branch["effects"])
                    for index, sequence in enumerate(sequences, 2):
                        self.assertEqual(len(sequence), index)
                        self.assertEqual(
                            [stage["generation"] for stage in sequence],
                            list(
                                range(
                                    sequence[0]["generation"],
                                    sequence[0]["generation"] + len(sequence),
                                )
                            ),
                        )
                        self.assertTrue(
                            all(stage["root_bytes"] > 0 for stage in sequence)
                        )
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
