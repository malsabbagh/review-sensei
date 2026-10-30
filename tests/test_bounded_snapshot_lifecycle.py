"""Complete candidate journey; 28 is a measured profile, not a publication cap."""

import copy
import tempfile
import unittest
from unittest.mock import PropertyMock, patch

from review_sensei_github.api_run_requests import ApiRunSessionLedger
from review_sensei_github.approval import ReviewApprovalEligibility
from review_sensei_github.native_union import native_union_frame_document
from review_sensei_github.publication import parse_finding_index
from review_sensei_github.session_ledger import GitHubIssueCommentSessionLedger
from test_quarantine_evidence import git
from test_quarantine_published_tail_journey import (
    BROKER_QUALIFICATIONS,
    HeavyPublishedTailJourneyFixture,
    NativePublishedTailJourneyFixture,
    registered_resource,
)

from review_sensei.api_work import ApiRunAccounting


class BoundedLifecycleFixture(
    NativePublishedTailJourneyFixture, HeavyPublishedTailJourneyFixture
):
    def select_command(self, body, *, new_run=False):
        if new_run:
            self.SOURCE_ID = 9_000_000 + self.run_id
        super().select_command(body, new_run=new_run)
        if new_run:
            self.extra_comments[self.source["id"]] = copy.deepcopy(self.source)


class BoundedSnapshotLifecycleTests(unittest.TestCase):
    def test_current_and_historical_decisions_finish_with_all_late_carriers(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(BROKER_QUALIFICATIONS, {}, clear=True),
        ):
            f = BoundedLifecycleFixture(directory)
            old, registration = f.original_origins()
            old_accounting = copy.deepcopy(old.api_run_accounting)
            # Existing audited frozen profile; production defaults stay intact.
            f.config_bytes = f.config_bytes.replace(
                b"api_request_limit: 512", b"api_request_limit: 1152"
            )
            (f.root / ".reviewsensei.yml").write_bytes(f.config_bytes)
            f.limit = 1152
            git(f.root, "add", ".reviewsensei.yml")
            git(
                f.root,
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-m",
                "Offline bounded profile",
            )
            f.policy = f.policy_ref = git(f.root, "rev-parse", "HEAD")
            f.creation_metadata = f.creator()
            f.provider_comments = [
                {
                    "path": "src/app.py",
                    "line": 3,
                    "side": "RIGHT",
                    "body": f"Independent current human decision {i} needs assessment.",
                    "severity": "high",
                    "blocking": False,
                    "needs_human": True,
                }
                for i in range(28)
            ]
            original_batch = ApiRunSessionLedger.stage_disposition_snapshot_batch
            batches = []

            def stage(ledger, identity, **kwargs):
                def count():
                    return sum(
                        "evidence-part:v1" in row["body"]
                        for row in f.extra_comments.values()
                    )

                before = count()
                manifest = original_batch(ledger, identity, **kwargs)
                added = count() - before
                batches.append((len(kwargs["values"]), added))
                self.assertLessEqual(added, 1)
                return manifest

            with (
                registered_resource([registration]),
                patch.object(
                    GitHubIssueCommentSessionLedger,
                    "supports_paged_disposition_snapshot_writes",
                    new_callable=PropertyMock,
                    return_value=True,
                ),
                patch.object(
                    ApiRunSessionLedger, "stage_disposition_snapshot_batch", stage
                ),
            ):
                self.assertEqual(f.invoke(), 0, f.error)
                provider_calls = f.provider_calls
                index = parse_finding_index(
                    f.reviews[-1]["body"],
                    repository_id=123,
                    pull_request=7,
                    head_sha=f.live_head,
                )
                self.assertEqual(len(index.entries), 28)
                late = None

                def command(body):
                    nonlocal late
                    f.select_command(body, new_run=True)
                    if late is None:
                        late = {
                            8_200_000 + i: copy.deepcopy(
                                f.extra_comments[8_200_000 + i]
                            )
                            for i in range(13)
                        }
                    self.assertEqual(f.invoke_command(), 0, f.error or str(f.exception))
                    accounting = ApiRunAccounting.from_document(
                        f.current_record().api_run_accounting
                    )
                    self.assertEqual(accounting.configuration.limit, 1152)
                    self.assertGreaterEqual(accounting.remaining, 0)
                    # Commands close their independent action result; the
                    # review-only terminal_transaction field is not their
                    # completion contract (see rollover_and_activate).
                    self.assertIsNotNone(accounting.action)
                    self.assertEqual(accounting.action["phase"], "terminal")
                    self.assertIsNotNone(accounting.action["result"])
                    self.assertEqual(old.api_run_accounting, old_accounting)
                    self.assertEqual(
                        {
                            identifier: f.extra_comments[identifier]
                            for identifier in late
                        },
                        late,
                    )

                for entry in index.entries:
                    command(
                        "@reviewsensei dismiss "
                        + entry.instance
                        + ' --reason "The exact current source proves this concern intentional."'
                    )
                self.assertEqual(
                    [count for count, added in batches if added], [8, 16, 24]
                )
                review = max(
                    (r for r in f.reviews if r["commit_id"] == f.live_head),
                    key=lambda r: r["id"],
                )
                pending = ReviewApprovalEligibility.from_dict(
                    native_union_frame_document(review["body"])["current"]
                ).quarantine_assessments["pending"]
                self.assertEqual(len(pending), 14)
                for item in pending:
                    command(
                        "@reviewsensei dismiss "
                        + item["instance"]
                        + ' --reason "quarantine:'
                        + item["origin_scope_sha256"]
                        + ' Original source evidence independently disproves this concern."'
                    )
                command("@reviewsensei verify")
                command("@reviewsensei verify")
                self.assertEqual(f.provider_calls, provider_calls)
                self.assertEqual(
                    sum(
                        r["state"] == "APPROVED" and r["commit_id"] == f.live_head
                        for r in f.reviews
                    ),
                    1,
                )
                self.assertIsNone(old.api_run_accounting["terminal_transaction"])
                self.assertLessEqual(len(f.extra_comments) + 1, 200)
