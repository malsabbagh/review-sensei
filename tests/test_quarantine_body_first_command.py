"""The original eighty-candidate body incident's first bounded decision."""

import inspect
import json
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from review_sensei_github import application
from review_sensei_github import quarantine_historical_execution as closed
from review_sensei_github.api_run_history import read_action_result
from review_sensei_github.api_run_requests import ApiRunRequestTransport
from review_sensei_github.quarantine_capacity import installed_capacity
from test_quarantine_body_limit import BodyLimitJourneyFixture
from test_quarantine_journey import registered_resource
from test_quarantine_published_tail_journey import BROKER_QUALIFICATIONS


class BodyPhysicalFixture(BodyLimitJourneyFixture):
    def __init__(self, directory, *, losses=False):
        super().__init__(directory)
        self.losses = losses
        self.started = False
        self.lost_roots = set()
        self.root_writes = {}
        self.receipt_lost = False
        self.puts = 0

    def external(self, request, timeout):
        method, path = request.get_method(), urlsplit(request.full_url).path
        body = json.loads(request.data) if request.data else {}
        if self.started and method == "PUT" and "/reviews/" in path:
            self.puts += 1
        snapshot = getattr(
            getattr(self, "accounted", None), "_quarantine_checkpoint_snapshot", None
        )
        root = (
            snapshot.root
            if self.started
            and snapshot is not None
            and path == f"/repos/owner/repo/issues/comments/{snapshot.comment_id}"
            else None
        )
        if root and method == "PATCH":
            self.root_writes[root] = self.root_writes.get(root, 0) + 1
        response = super().external(request, timeout)
        if (
            self.losses
            and root
            and method == "GET"
            and snapshot.operation.write_attempted
            and root not in self.lost_roots
        ):
            self.lost_roots.add(root)
            raise TimeoutError("synthetic C5 final root GET response loss")
        if (
            self.losses
            and self.started
            and method == "POST"
            and "reviewsensei:command-receipt:v1" in str(body.get("body", ""))
            and not self.receipt_lost
        ):
            self.receipt_lost = True
            raise TimeoutError("synthetic C5 stored receipt response loss")
        return response


class BodyFirstCommandTests(unittest.TestCase):
    def exercise(self, *, losses=False, one_short=False):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(BROKER_QUALIFICATIONS, {}, clear=True),
        ):
            f = BodyPhysicalFixture(directory, losses=losses)
            old, registration = f.original_body_failure()
            old_document = old.to_dict()
            with registered_resource([registration]):
                self.assertEqual(f.invoke(), 0, f.error)
                origin, inventory = next(
                    (k, v)
                    for k, v in f.selected.ledger._quarantine_authenticated.inventories.items()
                    if v["assessments"]
                )
                self.assertEqual(len(inventory["assessments"]), 23)
                f.quarantine_predecessor_comment_id = None
                f.select_command(
                    "@reviewsensei dismiss "
                    + inventory["assessments"][0]["instance"]
                    + ' --reason "quarantine:'
                    + origin
                    + ' Original source disproves only this historical concern."',
                    new_run=True,
                )
                issued, short, operations = [], [], []
                providers, reviews = f.provider_calls, len(f.reviews)
                operation_code = inspect.unwrap(
                    ApiRunRequestTransport._operation
                ).__code__

                def observe(frame, event, result):
                    if (
                        one_short
                        and event == "call"
                        and frame.f_code
                        is application.GitHubApplication._observe_quarantine_command_finalization.__code__
                    ):
                        run = frame.f_locals["run"]
                        self.assertEqual(
                            run.caller.budget_view.ordinary_remaining(), 110
                        )
                        run.http.request(
                            "GET",
                            run.http.repository_path(
                                run.identity.repository,
                                f"/pulls/{run.identity.pull_request}",
                            ),
                            token=run.ledger._quarantine_origin_reader.metadata_token,
                        )
                        self.assertEqual(
                            run.caller.budget_view.ordinary_remaining(), 109
                        )
                        short.append(run)
                    if (
                        event == "return"
                        and frame.f_code is closed.activate.__code__
                        and result is not None
                    ):
                        run = frame.f_locals["run"]
                        recipe = json.loads(frame.f_locals["entry"]["recipe"])
                        self.assertEqual(
                            recipe["contract"],
                            "closed-inline-c5-original-no-verification-v1",
                        )
                        self.assertEqual(
                            (
                                recipe["comment_reads"],
                                recipe["review_reads"],
                                recipe["combined_loss_calls"],
                            ),
                            (5, 3, 105),
                        )
                        self.assertEqual(recipe["source_review_reads"], 2)
                        minima = {r["phase"]: r for r in recipe["local_headroom"]}
                        self.assertEqual(
                            minima["review-update"],
                            {"phase": "review-update", "available": 35, "minimum": 35},
                        )
                        self.assertEqual(minima["receipt"]["available"], 8)
                        self.assertEqual(
                            run.caller.budget_view.ordinary_remaining(), 105
                        )
                        self.assertEqual(
                            len(
                                installed_capacity(
                                    run.ledger, run.identity
                                ).comment_plan.unknown
                            ),
                            16,
                        )
                        from review_sensei_github.quarantine_command_finalization import (
                            _default_c5_prefix_headroom,
                        )

                        from review_sensei.errors import ReviewInputError

                        stages = list(recipe["stages"])
                        stages[0] = (stages[0][0], stages[0][1] + 1)
                        with self.assertRaisesRegex(
                            ReviewInputError, "local reservation"
                        ):
                            _default_c5_prefix_headroom(
                                stages,
                                terminal_comments=recipe["terminal_comment_reads"],
                            )
                        guard = installed_capacity(run.ledger, run.identity)
                        certificate = guard.certificates["reviews"]
                        original_calls = certificate["physical_reads_upper_bound"]
                        try:
                            certificate["physical_reads_upper_bound"] = 4
                            # This is derived data, not authority: original
                            # installed_capacity recomputes it from sealed state.
                            closed.require_closed(result, run)
                            self.assertEqual(
                                guard.certificates["reviews"][
                                    "physical_reads_upper_bound"
                                ],
                                original_calls,
                            )
                        finally:
                            certificate["physical_reads_upper_bound"] = original_calls
                        closed.require_closed(result, run)
                        f.started = True
                        issued.append(run)
                    if (
                        f.started
                        and event == "return"
                        and frame.f_code is operation_code
                        and result is not None
                    ):
                        key = frame.f_locals["key"]
                        if key.startswith(
                            ("command-review-update:", "command-receipt:")
                        ):
                            operations.append(
                                (
                                    key.split(":")[0],
                                    result.capacity,
                                    frame.f_locals["self"].usable,
                                )
                            )

                previous = sys.getprofile()
                try:
                    sys.setprofile(observe)
                    rc = f.invoke_command()
                finally:
                    sys.setprofile(previous)
                self.assertEqual(rc, 1 if one_short else 0, f.error)
                self.assertEqual(old.to_dict(), old_document)
                self.assertEqual(f.provider_calls, providers)
                self.assertEqual(len(f.reviews), reviews)
                self.assertFalse(any(r.get("state") == "APPROVED" for r in f.reviews))
                run = short[0] if one_short else issued[0]
                state = run.caller.accounting
                blocks = {b.block_id: b for b in state.blocks}
                self.assertEqual(state.configuration.limit, 512)
                self.assertEqual(state.remaining, 190)
                self.assertFalse(
                    installed_capacity(run.ledger, run.identity).complete_tail
                )
                self.assertEqual(
                    len(
                        installed_capacity(
                            run.ledger, run.identity
                        ).comment_plan.unknown
                    ),
                    16,
                )
                if one_short:
                    self.assertEqual(issued, [])
                    self.assertIn(
                        "guarded suffix complete ordinary tail is unfunded", f.error
                    )
                    self.assertEqual(run.caller.budget_view.ordinary_remaining(), 104)
                    self.assertEqual(
                        blocks[run.caller._terminal_block].undispatched, 50
                    )
                    self.assertFalse(state.action["execution_started"])
                    self.assertEqual(f.puts, 0)
                    self.assertEqual(state.uncertain, 0)
                    return
                self.assertEqual(f.puts, 1)
                self.assertEqual(len(f.root_writes), 5)
                self.assertTrue(all(n == 1 for n in f.root_writes.values()))
                current = f.current_record()
                self.assertEqual(
                    current.api_run_accounting["action"]["phase"], "terminal"
                )
                retained = read_action_result(run.ledger, run.identity, current)
                self.assertEqual(
                    retained["outcome"], {"kind": "applied", "reason": None}
                )
                self.assertFalse(retained["result"]["evaluation_failed"])
                self.assertEqual(
                    [r["outcome"] for r in retained["result"]["receipts"]], ["recorded"]
                )
                self.assertEqual(
                    len(
                        f.selected.ledger._quarantine_authenticated.state["settlements"]
                    ),
                    1,
                )
                self.assertEqual(state.uncertain, 6 if losses else 0)
                self.assertEqual(
                    state.startup.liability
                    + len(state.completed)
                    + sum(b.undispatched + len(b.pending) for b in state.blocks)
                    + state.prior_nonstartup_liability
                    + state.remaining,
                    512,
                )
                receipts = [
                    row
                    for row in f.extra_comments.values()
                    if "reviewsensei:command-receipt:v1" in row.get("body", "")
                ]
                self.assertEqual(len(receipts), 1)
                self.assertEqual(
                    receipts[0]["user"]["id"], run.ledger._evidence_author_id
                )
                self.assertTrue(
                    any(
                        k == "command-review-update"
                        and capacity in (22, 35)
                        and available >= capacity
                        for k, capacity, available in operations
                    )
                )
                self.assertTrue(
                    any(
                        k == "command-receipt" and capacity == 4 and available >= 4
                        for k, capacity, available in operations
                    )
                )
                if losses:
                    self.assertEqual(len(f.lost_roots), 5)
                    self.assertTrue(f.receipt_lost)

    def test_original_body_first_decision_completes_without_new_funding(self):
        self.exercise()

    def test_original_body_first_decision_preserves_combined_loss_liabilities(self):
        self.exercise(losses=True)

    def test_original_body_first_decision_one_short_holds_before_effect(self):
        self.exercise(one_short=True)
