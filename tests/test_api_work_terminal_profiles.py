"""Terminal allocation boundaries and frozen-v1 public recovery compatibility."""

import copy
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch

from review_sensei_github.api_run_host import ApiRunReviewHooks
from review_sensei_github.api_run_plan import owner_allocations, terminal_requests
from review_sensei_github.approval import ReviewApprovalEligibility
from review_sensei_github.native_union import native_union_frame_document
from review_sensei_github.quarantine_published_tail import BROKER_QUALIFICATIONS
from review_sensei_github.session_ledger import (
    parse_session_comment,
    render_session_comment,
)
from test_api_work_persistence import BASE, HEAD, IDENTITY
from test_quarantine_activation import registered_resource
from test_quarantine_evidence import git
from test_quarantine_published_tail_journey import NativePublishedTailJourneyFixture
from test_review_run import EntryFixture

from review_sensei.api_work import (
    LEGACY_ACCOUNTING_VERSION,
    ApiRunAccounting,
    PendingApiRequest,
)


class TerminalAllocationProfileTests(unittest.TestCase):
    def test_each_owner_work_is_monotone_across_even_and_odd_boundaries(self):
        for startup in (130, 131, 132):
            previous = owner_allocations(511, startup)
            baseline = owner_allocations(512, startup)
            for limit in range(512, 551):
                with self.subTest(startup=startup, limit=limit):
                    current = owner_allocations(limit, startup)
                    self.assertEqual(
                        startup + current.first_total + current.successor_total,
                        limit,
                    )
                    self.assertGreaterEqual(current.first_work, previous.first_work)
                    self.assertGreaterEqual(
                        current.successor_work, previous.successor_work
                    )
                    self.assertGreaterEqual(current.first_work, baseline.first_work)
                    self.assertGreaterEqual(
                        current.successor_work, baseline.successor_work
                    )
                    previous = current
        expected = {
            512: (141, 50, 141, 50),
            513: (141, 51, 141, 50),
            514: (141, 51, 141, 51),
            548: (141, 68, 141, 68),
        }
        for limit, values in expected.items():
            actual = owner_allocations(limit, 130)
            self.assertEqual(
                (
                    actual.first_work,
                    actual.first_terminal,
                    actual.successor_work,
                    actual.successor_terminal,
                ),
                values,
            )

    def test_frozen_v1_keeps_original_split_at_every_limit(self):
        for limit in (512, 513, 514, 548, 1152):
            with self.subTest(limit=limit):
                allocation = owner_allocations(limit, 130, legacy=True)
                self.assertEqual(allocation.first_terminal, 50)
                self.assertEqual(allocation.successor_terminal, 50)
                self.assertEqual(allocation.successor_work, (limit - 130) // 2 - 50)
                self.assertEqual(
                    terminal_requests(limit, startup=130, successor=True, legacy=True),
                    50,
                )

    def test_current_publication_preserves_all_historical_holds_at_profile_boundaries(
        self,
    ):
        for limit in (512, 513, 514, 548):
            with (
                self.subTest(limit=limit),
                tempfile.TemporaryDirectory() as directory,
                patch.dict(BROKER_QUALIFICATIONS, {}, clear=True),
            ):
                fixture = NativePublishedTailJourneyFixture(directory)
                old, registration = fixture.original_origins()
                old_document = copy.deepcopy(old.api_run_accounting)
                # Change only the new run's synthetic trusted configuration;
                # the fourteen historical holds and old frozen policy remain.
                if limit != 512:
                    fixture.config_bytes = fixture.config_bytes.replace(
                        b"api_request_limit: 512",
                        f"api_request_limit: {limit}".encode(),
                    )
                    (fixture.root / ".reviewsensei.yml").write_bytes(
                        fixture.config_bytes
                    )
                    fixture.limit = limit
                    git(fixture.root, "add", ".reviewsensei.yml")
                    git(
                        fixture.root,
                        "-c",
                        "user.name=Fixture",
                        "-c",
                        "user.email=fixture@example.invalid",
                        "commit",
                        "-m",
                        "Offline terminal boundary configuration",
                    )
                    fixture.policy = fixture.policy_ref = git(
                        fixture.root, "rev-parse", "HEAD"
                    )
                    fixture.creation_metadata = fixture.creator()
                fixture.provider_comments = [
                    {
                        "path": "src/app.py",
                        "line": 3,
                        "side": "RIGHT",
                        "body": "Current published assessment 0 requires independent human judgment.",
                        "severity": "high",
                        "blocking": False,
                        "needs_human": True,
                    }
                ]
                with registered_resource([registration]):
                    self.assertEqual(fixture.invoke(), 0, fixture.error)
                self.assertEqual(old.api_run_accounting, old_document)
                observed = native_union_frame_document(fixture.reviews[-1]["body"])
                self.assertIsNotNone(observed)
                view = ReviewApprovalEligibility.from_dict(
                    observed["current"]
                ).quarantine_assessments
                self.assertEqual(len(view["pending"]), 14)
                self.assertTrue(view["fresh_human_pending"])
                current = fixture.accounted.accounting
                self.assertEqual(current.configuration.limit, limit)
                record = parse_session_comment(
                    fixture.comment["body"], identity=IDENTITY
                )
                self.assertEqual(record.transaction.phase, "publication_succeeded")
                self.assertFalse(any(r["state"] == "APPROVED" for r in fixture.reviews))

    def test_frozen_v1_public_successor_preserves_receipts_unknowns_and_work(self):
        for limit in (513, 548, 1152):
            with self.subTest(limit=limit), tempfile.TemporaryDirectory() as directory:
                fixture = EntryFixture(directory, limit=limit)

                def interrupted():
                    raise KeyboardInterrupt()

                fixture.before_publication = interrupted
                self.assertEqual(fixture.invoke(), 130, fixture.error)
                pending = parse_session_comment(
                    fixture.comment["body"], identity=IDENTITY
                )
                self.assertEqual(pending.transaction.phase, "publication_pending")
                current = ApiRunAccounting.from_document(pending.api_run_accounting)
                allocation = owner_allocations(limit, current.startup.liability)
                delta = allocation.first_terminal - 50
                # Fixture a historical v1 first-owner split. This is not a
                # runtime migration: freeze the complete old server document
                # before invoking the production successor, retaining receipts.
                blocks = tuple(
                    replace(block, undispatched=block.undispatched + delta)
                    if block.block_id == "hosted-review"
                    else replace(block, undispatched=block.undispatched - delta)
                    if block.block_id == "hosted-terminal"
                    else block
                    for block in current.blocks
                )
                old = replace(current, version=LEGACY_ACCOUNTING_VERSION, blocks=blocks)
                old = old.dispatch(
                    "hosted-review", PendingApiRequest("legacy-unknown-effect", 1)
                )
                data = pending.to_dict()
                data.pop("record_sha256")
                data.pop("schema_version")
                data["transaction"] = pending.transaction
                data["api_run_accounting"] = old.to_document()
                frozen = type(pending)._construct(**data)
                fixture.comment["body"] = render_session_comment(
                    repository_id=123, pull_request=7, record=frozen
                )
                old_document = copy.deepcopy(frozen.api_run_accounting)
                provider_guards = []

                def check_provider_tail():
                    # Retained-result recovery skips inference. Exercise its
                    # separate provider-tail guard before publication as well.
                    ledger = fixture.selected.ledger
                    hooks = ApiRunReviewHooks(
                        ledger, IDENTITY, base_sha=BASE, head_sha=HEAD
                    )
                    self.assertIs(hooks.begin_provider_phase(ledger), ledger)
                    provider_guards.append(True)

                fixture.before_publication = check_provider_tail
                fixture.attempt = 2
                self.assertEqual(fixture.invoke(), 0, fixture.error)
                self.assertEqual(provider_guards, [True])
                self.assertEqual(fixture.provider_calls, 1)
                after = fixture.accounted.accounting
                self.assertEqual(after.version, LEGACY_ACCOUNTING_VERSION)
                self.assertEqual(after.blocks[: len(old.blocks)], old.blocks)
                self.assertEqual(after.completed[: len(old.completed)], old.completed)
                self.assertEqual(after.uncertain, old.uncertain)
                self.assertEqual(frozen.api_run_accounting, old_document)
                self.assertEqual(after.configuration, old.configuration)
                self.assertEqual(after.startup, old.startup)
                self.assertEqual(after.remaining, 0)
                self.assertEqual(
                    next(
                        b for b in after.blocks if b.block_id == "successor-terminal"
                    ).issued_by,
                    after.owner,
                )
                final = parse_session_comment(
                    fixture.comment["body"], identity=IDENTITY
                )
                self.assertEqual(final.transaction.phase, "publication_succeeded")
                self.assertTrue(any(r["state"] == "APPROVED" for r in fixture.reviews))
