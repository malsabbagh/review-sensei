"""The real command producer, not caller flags, issues branch sizing custody."""

import copy
import tempfile
import unittest
from unittest.mock import patch

from review_sensei_github.publication import parse_finding_index

from review_sensei import finding_lifecycle_store as store
from review_sensei.errors import ReviewInputError
from tests.test_action_run import HEAD, CommandEntryFixture


class CommandBranchProofTests(unittest.TestCase):
    def test_actual_issued_branches_reject_copy_root_change_and_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = CommandEntryFixture(directory, limit=512)
            fixture.provider_comments = [
                dict(
                    path="src/app.py",
                    line=2,
                    side="RIGHT",
                    body="Confirm interface assumption.",
                    severity="high",
                    blocking=False,
                    needs_human=True,
                )
            ]
            self.assertEqual(fixture.invoke(), 0, fixture.error)
            index = parse_finding_index(
                fixture.reviews[-1]["body"],
                repository_id=123,
                pull_request=7,
                head_sha=HEAD,
            )
            fixture.select_command(
                "@reviewsensei override RS-"
                + index.entries[0].instance[:6].upper()
                + " acceptable risk",
                new_run=True,
            )
            original = store.preflight_finding_lifecycle
            checked = []

            def inspect(ledger, record, **kwargs):
                proof = kwargs.get("branch_documents")
                if proof is None:
                    return original(ledger, record, **kwargs)
                documents = (kwargs["document"], *kwargs["future_documents"])
                with self.assertRaises(ReviewInputError):
                    store._consume_checkpoint_branches(
                        copy.copy(proof), ledger, record, documents
                    )
                # A detached clone is not source-owned even with equal bytes.
                with self.assertRaises(ReviewInputError):
                    store._consume_checkpoint_branches(
                        proof, ledger, copy.deepcopy(record), documents
                    )
                # Failed consumption burns custody; it cannot be replayed against
                # the original record after trying a different root object.
                with self.assertRaises(ReviewInputError):
                    store._consume_checkpoint_branches(proof, ledger, record, documents)
                checked.append(True)
                raise ReviewInputError("test inspected branch custody before business")

            before = (
                fixture.business_writes,
                fixture.review_updates,
                set(fixture.resolved_threads),
            )
            with patch.object(store, "preflight_finding_lifecycle", inspect):
                self.assertEqual(fixture.invoke_command(), 0, fixture.error)
            self.assertEqual(checked, [True])
            self.assertIn("test inspected branch custody", fixture.stdout)
            self.assertEqual(
                (
                    fixture.business_writes,
                    fixture.review_updates,
                    set(fixture.resolved_threads),
                ),
                before,
            )
