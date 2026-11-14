"""Unconfirmed event creation cannot enter cursor/native/receipt successors."""

import inspect
import json
import unittest
from urllib.parse import urlsplit

import test_quarantine_command_checkpoint as checkpoint_tests
from review_sensei_github import quarantine_action


class EventLossFixture(checkpoint_tests.CommandCheckpointFixture):
    latest = None

    def __init__(self, directory, fault=None):
        super().__init__(directory, fault)
        type(self).latest = self
        self.event_posts = 0
        self.event_reads = 0
        self.event_body = None
        self.native_puts = 0
        self.receipt_posts = 0

    def external(self, request, timeout):
        frame = inspect.currentframe()
        while frame is not None and frame.f_code is not (
            quarantine_action._stage_decision_event.__code__
        ):
            frame = frame.f_back
        method = request.get_method()
        if frame is not None and method == "POST":
            self.event_posts += 1
            self.event_body = json.loads(request.data)["body"]
            if self.fault == "absent":
                self._meter(request)
                raise TimeoutError("synthetic absent event response loss")
            response = super().external(request, timeout)
            if self.fault == "stored":
                raise TimeoutError("synthetic stored event response loss")
            return response
        if (
            frame is not None
            and method == "GET"
            and self.event_posts
            and "/issues/comments/" in urlsplit(request.full_url).path
        ):
            self.event_reads += 1
            response = super().external(request, timeout)
            if self.fault == "echo":
                raise TimeoutError("synthetic event echo response loss")
            return response
        if self.event_posts:
            self.native_puts += method == "PUT"
            if request.data and "reviewsensei:command-receipt:v1" in (
                request.data.decode()
            ):
                self.receipt_posts += method in {"POST", "PATCH"}
        return super().external(request, timeout)


class EventLossTests(unittest.TestCase):
    def exercise(self, fault):
        checkpoint_tests.CommandCheckpointTests().exercise(
            fault, fixture_type=EventLossFixture
        )
        fixture = EventLossFixture.latest
        self.assertIsNotNone(fixture)
        self.assertEqual(fixture.event_posts, 1)
        self.assertEqual(fixture.event_reads, int(fault == "echo"))
        self.assertEqual(fixture.native_puts, 0)
        self.assertEqual(fixture.receipt_posts, 0)
        self.assertEqual(fixture.writes, {})
        self.assertEqual(
            sum(
                row.get("body") == fixture.event_body
                for row in fixture.extra_comments.values()
            ),
            int(fault != "absent"),
        )
        state = fixture.accounted.accounting
        self.assertGreaterEqual(state.uncertain, 1)
        self.assertEqual(state.remaining, 190)
        self.assertEqual(state.action["phase"], "pending")
        self.assertTrue(state.action["execution_started"])
        self.assertIsNone(state.action["result"])

    def test_absent_event_response_keeps_unknown_and_stops(self):
        self.exercise("absent")

    def test_stored_event_response_keeps_unknown_and_stops(self):
        self.exercise("stored")

    def test_lost_event_echo_keeps_unknown_and_stops(self):
        self.exercise("echo")
