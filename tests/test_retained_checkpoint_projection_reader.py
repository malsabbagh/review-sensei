"""Actual ledger reader forwards selected custody to the compact codec.

The descriptor is assembled in isolated native storage over a real accepted
operation source. This qualifies reader wiring, not compact-writer admission.
"""

import json
import tempfile
import unittest
from dataclasses import replace

from review_sensei_github.accepted_result import selected_accepted_result
from review_sensei_github.operation_comments import ResultPartStore
from review_sensei_github.publication_recovery import _event_key
from review_sensei_github.retention_projection import checkpoint_projection
from review_sensei_github.session_ledger import (
    GitHubIssueCommentSessionLedger,
    parse_session_comment,
)

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError
from review_sensei.models import ReviewResult, ReviewTransaction
from tests.fake_github_http import json_response, make_http
from tests.test_checkpoint_result_store import IsolatedCheckpointPort
from tests.test_review_run import EntryFixture


class RetainedCheckpointProjectionReaderTests(unittest.TestCase):
    def test_selected_transaction_and_app_identity_reach_real_compact_reader(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = EntryFixture(directory, limit=512)
            self.assertEqual(fixture.invoke(), 0, fixture.error)
            identity = fixture.selected.identity
            transaction = parse_session_comment(
                fixture.comment["body"], identity=identity
            ).transaction
            self.assertIsNotNone(transaction)
            accepted = selected_accepted_result(
                fixture.selected.http, transaction, app_id=999
            )
            bound = replace(
                ReviewResult.from_dict(json.loads(accepted.payload)),
                transaction=replace(transaction, phase="publication_pending"),
            )
            key = _event_key(identity, transaction)
            descriptor = checkpoint_projection(
                accepted.payload, bound, accepted.reference
            )
            for mode in ("selected", "wrong_transaction", "wrong_app", "missing_app"):
                with self.subTest(mode=mode):
                    stored = descriptor
                    if mode == "wrong_transaction":
                        foreign = ReviewTransaction.create(
                            **{
                                name: getattr(transaction, name)
                                for name in (
                                    "repository",
                                    "pull_request",
                                    "base_sha",
                                    "head_sha",
                                    "policy_digest",
                                    "configuration_digest",
                                    "evidence_digest",
                                )
                            },
                            reservation_id="0" * 64,
                            generation=transaction.generation,
                        ).with_result(transaction.result_sha256)
                        changed = json.loads(descriptor)
                        changed["transaction"] = foreign.to_dict()
                        stored = canonical_bytes(changed)
                    port = IsolatedCheckpointPort(fixture, key)
                    ResultPartStore(port, app_user_id=999).write(
                        event_key=key, result=stored, _check_existing=False
                    )
                    rows = [
                        {
                            "id": row.comment_id,
                            "body": row.body,
                            "user": {"id": row.user_id, "type": row.user_type},
                            "issue_url": "https://api.github.test/repos/owner/repo/issues/7",
                        }
                        for row in port.rows
                    ]
                    http, calls = make_http(
                        [
                            json_response(
                                {"id": 999, "login": "reviewsensei[bot]", "type": "Bot"}
                            ),
                            json_response(
                                {
                                    "state": "open",
                                    "base": {
                                        "sha": transaction.base_sha,
                                        "repo": {"id": 123, "full_name": "owner/repo"},
                                    },
                                    "head": {"sha": transaction.head_sha},
                                }
                            ),
                            json_response(rows),
                        ]
                    )
                    ledger = GitHubIssueCommentSessionLedger(
                        http, token="synthetic", app_slug="reviewsensei[bot]"
                    )
                    ledger.authenticated_app_id = (
                        None
                        if mode == "missing_app"
                        else 998
                        if mode == "wrong_app"
                        else 999
                    )
                    if mode == "selected":
                        restored = ledger.load_retained_checkpoint_result(
                            identity,
                            transaction=transaction,
                            head_sha=transaction.head_sha,
                        )
                        self.assertEqual(
                            restored.to_dict(),
                            replace(bound, transaction=transaction).to_dict(),
                        )
                    else:
                        with self.assertRaises(ReviewInputError):
                            ledger.load_retained_checkpoint_result(
                                identity,
                                transaction=transaction,
                                head_sha=transaction.head_sha,
                            )
                    self.assertEqual([method for method, _, _ in calls], ["GET"] * 3)
