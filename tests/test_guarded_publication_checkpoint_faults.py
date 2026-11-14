"""Physical root response losses in the actual guarded three-checkpoint route."""

import copy
import json
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from review_sensei_github.finding_lifecycle import (
    FindingLifecycleJournal,
    _unpack_journal,
)
from review_sensei_github.guarded_pending_publication import admit

from review_sensei.bounded_evidence import read_inline_evidence
from tests.test_quarantine_activation import registered_resource
from tests.test_quarantine_published_tail_journey import (
    BROKER_QUALIFICATIONS,
    PublishedTailJourneyFixture,
)


class GuardedPublicationCheckpointFaultTests(unittest.TestCase):
    def run_source(self, checkpoint, stored):
        active = []
        checkpoints = []
        attempts = []
        lost = []
        prior_reference = []
        admitted = []
        business = []
        code = FindingLifecycleJournal.checkpoint.__code__

        def observe(frame, event, value):
            if frame.f_code is admit.__code__:
                if event == "return" and value is True:
                    journal = frame.f_locals["journal"]
                    caller = journal.ledger.caller
                    terminal = next(
                        b
                        for b in caller.accounting.blocks
                        if b.block_id == caller._terminal_block
                    )
                    admitted.append((len(caller.observations), terminal.undispatched))
                return observe
            if frame.f_code is not code:
                return None
            if event == "call":
                journal = frame.f_locals["self"]
                document = copy.deepcopy(journal._document())
                if document["effects"]:
                    checkpoints.append(document)
                    active.append(len(checkpoints))
                else:
                    active.append(0)
            elif event == "return":
                active.pop()
            return observe

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(BROKER_QUALIFICATIONS, {}, clear=True),
        ):
            fixture = PublishedTailJourneyFixture(directory)
            _, registration = fixture.original_origins(
                failed_count=1, published_count=1
            )
            previous_root = copy.deepcopy(fixture.comment)
            previous_reviews = copy.deepcopy(fixture.reviews)
            previous_checks = copy.deepcopy(fixture.check_runs)
            external = fixture.external

            def service(request, timeout):
                path = urlsplit(request.full_url).path
                body = json.loads(request.data) if request.data else {}
                if request.get_method() in ("POST", "PATCH", "PUT") and (
                    "/check-runs" in path or path.endswith("/reviews")
                ):
                    self.assertEqual(len(admitted), 1)
                    self.assertGreater(admitted[0][1], 0)
                    business.append((request.get_method(), path, body))
                if (
                    active
                    and active[-1]
                    and request.get_method() == "PATCH"
                    and "/issues/comments/" in path
                    and "reviewsensei:session:v2" in body.get("body", "")
                ):
                    attempts.append((active[-1], path, copy.deepcopy(body)))
                    if active[-1] == checkpoint and not lost:
                        lost.append(len(fixture.accounted.observations) - 1)
                        prior_reference.append(
                            copy.deepcopy(fixture.current_record().finding_lifecycle)
                        )
                        terminal = next(
                            b
                            for b in fixture.accounted.accounting.blocks
                            if b.block_id == fixture.accounted._terminal_block
                        )
                        self.assertEqual(terminal.undispatched, admitted[0][1])
                        if stored:
                            external(request, timeout)
                        else:
                            fixture._meter(request)
                        raise TimeoutError("injected guarded root response loss")
                return external(request, timeout)

            fixture.external = service
            prior_trace = sys.gettrace()
            try:
                sys.settrace(observe)
                with registered_resource([registration]):
                    result = fixture.invoke()
            finally:
                sys.settrace(prior_trace)
            self.assertEqual(fixture.comment, previous_root)
            observations = copy.deepcopy(fixture.accounted.observations)
            trace = [row for row in fixture.trace if row[0] == fixture.attempt]
            self.assertEqual(len(observations), len(trace))
            self.assertEqual(len(lost), 1, fixture.error)
            self.assertEqual(sum(row[0] == checkpoint for row in attempts), 1)
            accounting = fixture.accounted.accounting
            self.assertEqual(accounting.configuration.limit, 512)
            self.assertEqual(
                accounting.remaining
                + accounting.startup.liability
                + accounting.prior_nonstartup_liability
                + accounting.confirmed
                + accounting.reserved_undispatched
                + accounting.uncertain,
                512,
            )
            lost_id = observations[lost[0]]["request_id"]
            charged = [item.request_id for item in accounting.completed] + [
                item.request_id for block in accounting.blocks for item in block.pending
            ]
            self.assertEqual(charged.count(lost_id), 1)
            record = fixture.current_record()
            reference = record.finding_lifecycle
            retained = None
            if reference is not None:
                manifest = reference["manifest"]
                retained = _unpack_journal(
                    read_inline_evidence(
                        manifest, expected_binding=manifest["binding"]
                    )["lifecycle"]
                )
            return dict(
                code=result,
                error=fixture.error,
                checkpoints=checkpoints,
                attempts=attempts,
                business=business,
                admitted=admitted,
                observations=observations,
                lost=lost,
                reviews=copy.deepcopy(fixture.reviews),
                old_reviews=previous_reviews,
                checks=copy.deepcopy(fixture.check_runs),
                old_checks=previous_checks,
                accounting=fixture.accounted.accounting,
                record=record,
                retained=retained,
                prior_reference=prior_reference[0],
            )

    def test_root_checkpoint_response_loss_matrix(self):
        # Counts include every caller request, not just native mutations. Stored
        # responses reconcile the exact CAS; absence never permits blind retry.
        absent_calls = {1: 134, 2: 143, 3: 157}
        for checkpoint in (1, 2, 3):
            for stored in (False, True):
                with self.subTest(checkpoint=checkpoint, stored=stored):
                    result = self.run_source(checkpoint, stored)
                    self.assertEqual(
                        result["code"], 0 if stored else 1, result["error"]
                    )
                    self.assertEqual(
                        len(result["observations"]),
                        171 if stored else absent_calls[checkpoint],
                    )
                    self.assertEqual(
                        [row[0] for row in result["attempts"]],
                        list(range(1, (3 if stored else checkpoint) + 1)),
                    )
                    native_count = 3 if stored or checkpoint == 3 else checkpoint - 1
                    self.assertEqual(len(result["business"]), native_count)
                    lost = result["observations"][result["lost"][0]]
                    self.assertEqual(lost["outcome"], "unknown")
                    self.assertTrue(lost["mutation"])
                    if stored:
                        self.assertEqual(
                            [e["status"] for e in result["retained"]["effects"]],
                            ["confirmed"] * 3,
                        )
                        self.assertEqual(
                            len(result["reviews"]), len(result["old_reviews"]) + 1
                        )
                    else:
                        self.assertEqual(
                            result["record"].finding_lifecycle,
                            result["prior_reference"],
                        )
                    if checkpoint == 1 and not stored:
                        self.assertEqual(result["reviews"], result["old_reviews"])
                        self.assertEqual(result["checks"], result["old_checks"])
                    if checkpoint == 2 and not stored:
                        self.assertEqual(result["reviews"], result["old_reviews"])
                        self.assertEqual(
                            [e["status"] for e in result["retained"]["effects"]],
                            ["pending"],
                        )
                        self.assertIsNone(result["retained"]["effects"][0]["response"])
                    if checkpoint == 3 and not stored:
                        retained = result["retained"]
                        self.assertEqual(
                            retained["schema_version"], "finding-lifecycle-journal-v3"
                        )
                        self.assertIsNotNone(retained["guarded_publication_pair"])
                        self.assertEqual(
                            [e["status"] for e in retained["effects"]],
                            ["confirmed", "pending", "pending"],
                        )
                        for effect in retained["effects"][1:]:
                            self.assertIsNone(effect["response"])
                        self.assertEqual(
                            len(result["reviews"]), len(result["old_reviews"]) + 1
                        )
