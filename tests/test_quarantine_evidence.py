"""Actual CLI retention is reconstructed without inference or historical writes."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit

from fake_github_http import json_response
from review_sensei_github.quarantine_evidence import (
    CurrentReadScope,
    read_published_source,
    reconstruct_failed_source,
)
from review_sensei_github.session_ledger import parse_session_comment
from test_review_run import EntryFixture


def git(root, *args):
    return (
        subprocess.check_output(
            [
                "git",
                "-C",
                str(root),
                "-c",
                "core.hooksPath=" + os.devnull,
                "-c",
                "core.autocrlf=false",
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                *args,
            ],
            stderr=subprocess.DEVNULL,
        )
        .decode()
        .strip()
    )


class FailedPublicationFixture(EntryFixture):
    def external(self, request, timeout):
        path = urlsplit(request.full_url).path
        if request.method == "POST" and path.endswith("/pulls/7/reviews"):
            self.trace.append((self.attempt, request.method, path))
            return json_response({"message": "synthetic publication refusal"}, 403)
        return super().external(request, timeout)


def original_objects(fixture):
    root = fixture.root
    (root / "src").mkdir()
    (root / "src/app.py").write_text("keep\n")
    git(root, "add", "src/app.py")
    git(root, "commit", "-m", "original base")
    base = git(root, "rev-parse", "HEAD")
    (root / "src/app.py").write_text("keep\nchange\n")
    git(root, "add", "src/app.py")
    git(root, "commit", "-m", "reviewed head")
    head = git(root, "rev-parse", "HEAD")
    diff = subprocess.check_output(
        ["git", "-C", str(root), "diff", base + "..." + head]
    ).decode()
    git(root, "checkout", "--detach", base)
    fixture.policy = fixture.policy_ref = base
    fixture.live_base = fixture.requested_base = base
    fixture.live_head = fixture.requested_head = head
    (root / "review.patch").write_text(diff, encoding="utf-8", newline="\n")


class QuarantineEvidenceTests(unittest.TestCase):
    def test_current_failed_publication_preserves_original_admission_for_retry(self):
        class Recoverable(FailedPublicationFixture):
            allow = False

            def external(self, request, timeout):
                if (
                    self.allow
                    and request.method == "POST"
                    and urlsplit(request.full_url).path.endswith("/pulls/7/reviews")
                ):
                    return EntryFixture.external(self, request, timeout)
                return super().external(request, timeout)

        with tempfile.TemporaryDirectory() as directory:
            fixture = Recoverable(directory)
            original_objects(fixture)
            self.assertFalse(fixture.legacy_publication_fixture)
            self.assertEqual(fixture.invoke(human=True), 1)
            previous = parse_session_comment(
                fixture.comment["body"], identity=fixture.selected.identity
            )
            self.assertEqual(previous.transaction.phase, "publication_failed")
            self.assertIsNone(previous.api_run_accounting["terminal_transaction"])
            calls = fixture.provider_calls
            fixture.allow, fixture.attempt = True, 2
            from review_sensei_github.finding_lifecycle import restore_finding_lifecycle

            from review_sensei.finding_lifecycle import decode_lifecycle_state

            admitted = []

            def observe(frame, event, result):
                # The durable lifecycle is restored before retrying the native
                # effect. Observe that actual authenticated return, without
                # replacing a reader or demanding the obsolete result fallback.
                if (
                    event == "return"
                    and frame.f_code is restore_finding_lifecycle.__code__
                    and result is not None
                    and result[1] is not None
                ):
                    admitted.append(
                        (
                            result[0].finding_lifecycle,
                            decode_lifecycle_state(result[1]["lifecycle"]).state,
                        )
                    )

            previous_profile = sys.getprofile()
            try:
                sys.setprofile(observe)
                self.assertEqual(fixture.invoke(human=True), 1, fixture.error)
            finally:
                sys.setprofile(previous_profile)
            self.assertTrue(admitted)
            self.assertEqual(admitted[0][0], previous.finding_lifecycle)
            inventories = [
                (
                    getattr(state, "inventory", None)
                    or state.authoritative_decision_inventory
                ).obligations
                for _, state in admitted
            ]
            original_criteria = {
                item.obligation_id: item.criterion for item in inventories[0]
            }
            self.assertTrue(original_criteria)
            for obligations in inventories:
                self.assertEqual(
                    {item.obligation_id: item.criterion for item in obligations},
                    original_criteria,
                )
            self.assertTrue(
                all(
                    any(
                        obligation.criterion.needs_human and obligation.unresolved
                        for obligation in obligations
                    )
                    for obligations in inventories
                )
            )
            self.assertIn(
                "native outcome remains uncertain; no retry dispatched", fixture.error
            )
            current = parse_session_comment(
                fixture.comment["body"], identity=fixture.selected.identity
            )
            self.assertEqual(fixture.provider_calls, calls)
            self.assertEqual(
                current.transaction.transaction_id, previous.transaction.transaction_id
            )
            self.assertEqual(current.transaction.phase, "publication_failed")
            self.assertIsNone(current.api_run_accounting["terminal_transaction"])
            self.assertEqual(fixture.reviews, [])

    def test_actual_retained_failure_reconstructs_original_admission_without_effects(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            fixture = FailedPublicationFixture(directory)
            original_objects(fixture)
            self.assertEqual(fixture.invoke(human=True), 1)
            selected = fixture.selected
            previous = parse_session_comment(
                fixture.comment["body"], identity=selected.identity
            )
            self.assertEqual(
                previous.transaction.phase, "publication_failed", fixture.error
            )
            before_root = fixture.comment["body"]
            before_provider = fixture.provider_calls
            before = len(fixture.trace)
            scope = CurrentReadScope(
                selected.ledger,
                selected.identity,
                fixture.live_base,
                fixture.live_head,
                "synthetic-workflow-read",
            )
            cwd = Path.cwd()
            try:
                os.chdir(fixture.root)
                source, inventory, result = reconstruct_failed_source(
                    ledger=selected.ledger, previous=previous, current_scope=scope
                )
            finally:
                os.chdir(cwd)
            self.assertEqual(fixture.comment["body"], before_root)
            self.assertEqual(fixture.provider_calls, before_provider)
            self.assertEqual(len(result.comments), 1)
            self.assertEqual(len(inventory["assessments"]), 1)
            self.assertEqual(
                source["predecessor_record_sha256"], previous.record_sha256
            )
            self.assertTrue(
                all(method == "GET" for _, method, _ in fixture.trace[before:])
            )
            self.assertIsNone(previous.api_run_accounting["terminal_transaction"])

    def test_actual_earlier_published_inventory_is_read_from_retained_root_without_completion(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            fixture = EntryFixture(directory)
            original_objects(fixture)
            self.assertEqual(fixture.invoke(human=True), 0, fixture.error)
            first = parse_session_comment(
                fixture.comment["body"], identity=fixture.selected.identity
            )
            self.assertEqual(first.transaction.phase, "publication_succeeded")
            fixture.next_run()

            def stop():
                raise KeyboardInterrupt()

            fixture.before_provider = stop
            self.assertEqual(fixture.invoke(), 130, fixture.error)
            selected = fixture.selected
            previous = parse_session_comment(
                fixture.comment["body"], identity=selected.identity
            )
            before = fixture.comment["body"]
            before_calls = len(fixture.trace)
            scope = CurrentReadScope(
                selected.ledger,
                selected.identity,
                fixture.live_base,
                fixture.live_head,
                "synthetic-workflow-read",
            )
            source, inventory = read_published_source(
                ledger=selected.ledger, previous=previous, current_scope=scope
            )
            self.assertEqual(source["record"]["record_sha256"], first.record_sha256)
            self.assertEqual(len(inventory["assessments"]), 1)
            self.assertEqual(fixture.comment["body"], before)
            self.assertTrue(
                all(method == "GET" for _, method, _ in fixture.trace[before_calls:])
            )
