"""Isolated native-root custody does not replace frozen predecessor evidence."""

import copy
import sys
import tempfile
import unittest
from unittest.mock import patch

from review_sensei_github import quarantine_census as census
from review_sensei_github import quarantine_selected_predecessor as custody
from review_sensei_github import quarantine_selection
from review_sensei_github.session_ledger import render_session_comment

from review_sensei.errors import ReviewInputError
from review_sensei.source_registry import require_module_seal
from tests.test_quarantine_journey import registered_resource
from tests.test_quarantine_published_tail_journey import (
    BROKER_QUALIFICATIONS,
    PublishedTailJourneyFixture,
)


class SelectedPredecessorTests(unittest.TestCase):
    def test_direct_consumers_cannot_issue_or_read_custody(self):
        require_module_seal(custody)
        require_module_seal(quarantine_selection)
        for function in (custody.capture, custody.selected_digest):
            with self.assertRaisesRegex(ReviewInputError, "original"):
                function(object(), object(), object(), [])

    def test_original_selection_pins_two_roots_without_promoting_old_body(self):
        checked = []
        observations = []
        callers = []
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(BROKER_QUALIFICATIONS, {}, clear=True),
        ):
            f = PublishedTailJourneyFixture(directory)
            old, registration = f.original_origins(failed_count=1, published_count=1)
            archived = old.to_dict()
            with registered_resource([registration]):
                self.assertEqual(f.invoke(), 0, f.error)
                prior_authenticated = f.selected.ledger._quarantine_authenticated
                origin, inventory = next(
                    (key, value)
                    for key, value in f.selected.ledger._quarantine_authenticated.inventories.items()
                    if value["assessments"]
                )
                late = old.evolve(generation=old.generation + 1)
                f.comment["body"] = render_session_comment(
                    repository_id=123, pull_request=7, record=late
                )
                f.quarantine_predecessor_comment_id = None
                f.select_command(
                    "@reviewsensei dismiss "
                    + inventory["assessments"][0]["instance"]
                    + ' --reason "quarantine:'
                    + origin
                    + ' Original source disproves this historical concern."',
                    new_run=True,
                )

                providers, review_count = f.provider_calls, len(f.reviews)

                def observe(frame, event, result):
                    if event == "return" and frame.f_code in (
                        custody.capture.__code__,
                        custody.selected_digest.__code__,
                    ):
                        observations.append(
                            (frame.f_code.co_name, result, frame.f_lineno)
                        )
                    if (
                        checked
                        or event != "return"
                        or frame.f_code is not custody.selected_digest.__code__
                    ):
                        return
                    predecessor = frame.f_locals["predecessor"]
                    if result is None or result[0] == predecessor.record_sha256:
                        return
                    checked.append(True)
                    ledger = frame.f_locals["ledger"]
                    callers.append(ledger.caller)
                    identity = frame.f_locals["identity"]
                    authenticated = frame.f_locals["authenticated"]
                    self.assertEqual(result[0], late.record_sha256)
                    self.assertEqual(predecessor.to_dict(), archived)
                    self.assertNotEqual(result[0], predecessor.record_sha256)
                    rows = copy.deepcopy(ledger._comments_by_id)
                    order = list(ledger._comment_order)
                    old_id = authenticated.lineage["predecessor_comment_id"]
                    current_id = result[1]
                    before = ledger.caller.accounting.to_document()
                    cases = []
                    missing = copy.deepcopy(rows)
                    missing.pop(old_id)
                    cases.append(("missing", missing))
                    foreign = copy.deepcopy(rows)
                    foreign[old_id]["user"]["id"] += 1
                    cases.append(("foreign", foreign))
                    extra = copy.deepcopy(rows)
                    extra[99999999] = {**extra[old_id], "id": 99999999}
                    cases.append(("extra", extra))
                    changed = copy.deepcopy(rows)
                    changed[old_id]["body"] = render_session_comment(
                        repository_id=123,
                        pull_request=7,
                        record=late.evolve(generation=late.generation + 1),
                    )
                    cases.append(("unseen old", changed))
                    missing_current = copy.deepcopy(rows)
                    missing_current.pop(current_id)
                    cases.append(("missing current", missing_current))
                    changed_current = copy.deepcopy(rows)
                    changed_current[current_id]["body"] = render_session_comment(
                        repository_id=123,
                        pull_request=7,
                        record=ledger.caller._effect_record.evolve(
                            generation=ledger.caller._effect_record.generation + 1
                        ),
                    )
                    cases.append(("changed current", changed_current))
                    for label, replacement in cases:
                        with (
                            self.subTest(case=label),
                            patch.object(ledger, "_comments_by_id", replacement),
                            patch.object(ledger, "_comment_order", list(replacement)),
                        ):
                            with self.assertRaises(ReviewInputError):
                                census.lifecycle_capacity_plan(
                                    ledger, identity, added_parts=0
                                )
                    for label, replacement in (
                        ("copied", copy.copy(authenticated)),
                        ("previous invocation", prior_authenticated),
                    ):
                        with (
                            self.subTest(custody=label),
                            patch.object(
                                ledger, "_quarantine_authenticated", replacement
                            ),
                        ):
                            with self.assertRaises(ReviewInputError):
                                census.lifecycle_capacity_plan(
                                    ledger, identity, added_parts=0
                                )
                    owner = authenticated.original_caller
                    try:
                        object.__setattr__(authenticated, "original_caller", object())
                        with self.assertRaises(ReviewInputError):
                            census.lifecycle_capacity_plan(
                                ledger, identity, added_parts=0
                            )
                    finally:
                        object.__setattr__(authenticated, "original_caller", owner)
                    callbacks = []
                    with patch.object(
                        custody, "selected_digest", lambda *args: callbacks.append(True)
                    ):
                        with self.assertRaisesRegex(ReviewInputError, "source changed"):
                            census.lifecycle_capacity_plan(
                                ledger, identity, added_parts=0
                            )
                    self.assertEqual(callbacks, [])

                    class Hostile:
                        def __eq__(self, other):
                            callbacks.append("equality")
                            raise AssertionError("hostile field equality")

                        def __getattribute__(self, name):
                            callbacks.append(name)
                            raise AssertionError("hostile field attribute")

                    for name in (
                        "record_sha256",
                        "lineage_bytes",
                        "archive_bytes",
                        "state_bytes",
                    ):
                        original = vars(authenticated)[name]
                        try:
                            object.__setattr__(authenticated, name, Hostile())
                            with self.assertRaises(ReviewInputError):
                                census.lifecycle_capacity_plan(
                                    ledger, identity, added_parts=0
                                )
                        finally:
                            object.__setattr__(authenticated, name, original)
                    for target in (identity, authenticated.original_identity):
                        original = vars(target)["repository"]
                        try:
                            object.__setattr__(target, "repository", Hostile())
                            with self.assertRaises(ReviewInputError):
                                census.lifecycle_capacity_plan(
                                    ledger, identity, added_parts=0
                                )
                        finally:
                            object.__setattr__(target, "repository", original)
                    record = ledger.caller._effect_record
                    object.__setattr__(
                        record, "to_dict", lambda: callbacks.append("serializer")
                    )
                    try:
                        with self.assertRaises(ReviewInputError):
                            census.lifecycle_capacity_plan(
                                ledger, identity, added_parts=0
                            )
                    finally:
                        object.__delattr__(record, "to_dict")
                    original_lineage = record.quarantine_lineage
                    try:
                        object.__setattr__(
                            record, "quarantine_lineage", {"nested": [Hostile()]}
                        )
                        with self.assertRaises(ReviewInputError):
                            census.lifecycle_capacity_plan(
                                ledger, identity, added_parts=0
                            )
                    finally:
                        object.__setattr__(
                            record, "quarantine_lineage", original_lineage
                        )
                    self.assertEqual(callbacks, [])
                    with self.assertRaisesRegex(ReviewInputError, "original selection"):
                        custody.capture(
                            ledger, identity, ledger.caller._effect_record, []
                        )
                    self.assertEqual(ledger._comment_order, order)
                    self.assertEqual(ledger.caller.accounting.to_document(), before)

                previous = sys.getprofile()
                try:
                    sys.setprofile(observe)
                    rc = f.invoke_command()
                finally:
                    sys.setprofile(previous)
                self.assertEqual(rc, 0, f.error + str(observations))
                self.assertEqual(checked, [True])
                self.assertEqual(f.provider_calls, providers)
                self.assertEqual(len(f.reviews), review_count)
                self.assertFalse(any(row["state"] == "APPROVED" for row in f.reviews))
                self.assertEqual(
                    f.comment["body"],
                    render_session_comment(
                        repository_id=123, pull_request=7, record=late
                    ),
                )
                current = f.current_record()
                state = callers[0].accounting
                self.assertEqual(
                    current.api_run_accounting["action"]["phase"], "terminal"
                )
                self.assertEqual(
                    len(
                        f.selected.ledger._quarantine_authenticated.state["settlements"]
                    ),
                    1,
                )
                self.assertEqual(state.configuration.limit, 512)
                self.assertEqual(state.uncertain, 0)
                self.assertEqual(state.remaining, 190)
                self.assertEqual(
                    state.startup.liability
                    + len(state.completed)
                    + sum(
                        block.undispatched + len(block.pending)
                        for block in state.blocks
                    )
                    + state.prior_nonstartup_liability
                    + state.remaining,
                    512,
                )
                self.assertEqual(old.to_dict(), archived)
