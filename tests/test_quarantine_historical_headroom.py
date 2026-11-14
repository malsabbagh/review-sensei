"""A real paid read cannot be refunded to admit an unfunded suffix."""

import sys
import tempfile
import unittest
from unittest.mock import patch

from review_sensei_github import application
from review_sensei_github import quarantine_command_suffix as suffix
from test_quarantine_historical_execution import GuardedHistoricalFixture
from test_quarantine_journey import registered_resource
from test_quarantine_published_tail_journey import BROKER_QUALIFICATIONS


class HistoricalHeadroomTests(unittest.TestCase):
    def test_actual_extra_paid_read_refuses_one_short_before_command_effect(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(BROKER_QUALIFICATIONS, {}, clear=True),
        ):
            fixture = GuardedHistoricalFixture(directory)
            _, registration = fixture.original_origins(
                failed_count=1, published_count=1
            )
            captured = []
            with registered_resource([registration]):
                self.assertEqual(fixture.invoke(), 0, fixture.error)
                origin, inventory = next(
                    (k, v)
                    for k, v in fixture.selected.ledger._quarantine_authenticated.inventories.items()
                    if v["assessments"]
                )
                fixture.quarantine_predecessor_comment_id = None
                fixture.select_command(
                    "@reviewsensei dismiss "
                    + inventory["assessments"][0]["instance"]
                    + ' --reason "quarantine:'
                    + origin
                    + ' Exact source disproves this historical concern."',
                    new_run=True,
                )
                before_comments = {
                    k: dict(v) for k, v in fixture.extra_comments.items()
                }

                def observe(frame, event, result):
                    if (
                        event == "call"
                        and frame.f_code
                        is application.GitHubApplication._observe_quarantine_command_finalization.__code__
                    ):
                        before_comments.update(
                            {k: dict(v) for k, v in fixture.extra_comments.items()}
                        )
                        run = frame.f_locals["run"]
                        self.assertEqual(
                            run.caller.budget_view.ordinary_remaining(), 110
                        )
                        start = len(run.caller.observations)
                        status, _ = run.http.request(
                            "GET",
                            run.http.repository_path(
                                run.identity.repository,
                                f"/pulls/{run.identity.pull_request}",
                            ),
                            token=run.ledger._quarantine_origin_reader.metadata_token,
                        )
                        self.assertEqual(status, 200)
                        self.assertEqual(len(run.caller.observations), start + 1)
                        self.assertEqual(
                            run.caller.budget_view.ordinary_remaining(), 109
                        )
                    if (
                        event == "return"
                        and frame.f_code is suffix.prepare.__code__
                        and "before_preparation" in frame.f_locals
                    ):
                        run = frame.f_locals["run"]
                        self.assertIsNone(result)
                        self.assertEqual(frame.f_locals["ordinary_before"], 110)
                        self.assertEqual(
                            run.caller.budget_view.ordinary_remaining(), 104
                        )
                        self.assertEqual(
                            frame.f_locals["recipe"]["combined_loss_calls"], 105
                        )
                        self.assertEqual(
                            len(run.caller.observations)
                            - frame.f_locals["before_preparation"],
                            6,
                        )
                        state = run.caller.accounting
                        blocks = {b.block_id: b for b in state.blocks}
                        self.assertEqual(state.configuration.limit, 512)
                        self.assertEqual(state.remaining, 190)
                        self.assertEqual(
                            blocks[run.caller._terminal_block].undispatched, 50
                        )
                        self.assertEqual(state.uncertain, 0)
                        captured.append(run)

                previous = sys.getprofile()
                try:
                    sys.setprofile(observe)
                    result = fixture.invoke_command()
                finally:
                    sys.setprofile(previous)
                self.assertEqual(result, 1, fixture.error)
                self.assertIn(
                    "guarded suffix complete ordinary tail is unfunded", fixture.error
                )
                self.assertEqual(len(captured), 1)
                self.assertFalse(fixture.closed_started)
                self.assertEqual(fixture.native_puts, 0)
                self.assertEqual(set(fixture.extra_comments), set(before_comments))
                from review_sensei_github.session_ledger import parse_session_comment

                run = captured[0]
                for key, prior in before_comments.items():
                    if (
                        parse_session_comment(
                            prior.get("body", ""), identity=run.identity
                        )
                        is None
                    ):
                        self.assertEqual(fixture.extra_comments[key], prior)
                self.assertFalse(run.caller.accounting.action["execution_started"])
                self.assertEqual(run.caller.accounting.action["phase"], "pending")
