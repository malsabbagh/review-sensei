"""Source-produced initial-publication sizing never installs a live prefix."""

import copy
import hashlib
import itertools
import tempfile
import unittest
from unittest.mock import patch

from review_sensei_github import ordinary_publication_plan
from review_sensei_github.ordinary_publication_plan import price_prefixes
from review_sensei_github.publication import ReviewPublisher

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.models import ReviewComment
from tests import test_recovery_context_g17 as recovery


class OrdinaryPublicationPlanTests(unittest.TestCase):
    def test_review_event_uses_same_actual_approval_and_author_rules(self):
        changes = {
            (True, True, False, False),
            (True, False, True, False),
            (True, True, True, False),
        }
        for case in itertools.product((False, True), repeat=4):
            with self.subTest(case=case):
                self.assertEqual(
                    ReviewPublisher._ordinary_review_event(
                        approval_enabled=case[0],
                        requests_changes=case[1],
                        retained_blocking=case[2],
                        app_authored=case[3],
                    ),
                    ("REQUEST_CHANGES", "CHANGES_REQUESTED")
                    if case in changes
                    else ("COMMENT", "COMMENTED"),
                )

    def test_inline_payload_preserves_actual_prepared_prose_and_anchor(self):
        comment = ReviewComment(path="src/app.py", line=12, body="Original criterion")
        for anchor, side in (("left", "LEFT"), ("right", "RIGHT")):
            with self.subTest(anchor=anchor):
                self.assertEqual(
                    ReviewPublisher._inline_comment_payload(
                        comment, "Authenticated rendered criterion and marker", anchor
                    ),
                    {
                        "path": "src/app.py",
                        "body": "Authenticated rendered criterion and marker",
                        "line": 12,
                        "side": side,
                    },
                )

    def test_private_prefix_envelopes_leave_actual_source_and_meter_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = (
                recovery.OrdinaryEntryHostedAndCliRecoveryG17Tests._make_hosted_fixture(
                    directory
                )
            )
            original = ordinary_publication_plan.admit_current
            observed = []

            def inspect(publisher, observer, journal, **kwargs):
                if journal is not None and not observed:
                    record = canonical_bytes(journal._record.to_dict())
                    effects = copy.deepcopy(journal.effects)
                    trace = list(fixture.trace)
                    budget = journal.ledger.evidence_budget
                    remaining = budget.ordinary_remaining()
                    repository = journal.identity.repository
                    requests = (
                        (
                            "POST",
                            f"/repos/{repository}/check-runs",
                            {
                                "head_sha": journal.head_sha,
                                "name": "ReviewSensei",
                                "status": "in_progress",
                            },
                            None,
                        ),
                        (
                            "POST",
                            f"/repos/{repository}/pulls/{journal.identity.pull_request}/reviews",
                            {
                                "commit_id": journal.head_sha,
                                "event": "COMMENT",
                                "body": "Sizing boundary only, never submitted",
                                "comments": [],
                            },
                            None,
                        ),
                    )
                    documents, count = price_prefixes(journal, requests, native_calls=4)
                    from review_sensei_github.finding_lifecycle import _unpack_journal
                    from review_sensei_github.publication import (
                        MAX_PUBLISHED_REVIEW_BODY_BYTES,
                        _framed_review_body,
                        render_check_warning,
                    )

                    for warning in (
                        "",
                        render_check_warning("check_permission", concise=True),
                    ):
                        rendered = _framed_review_body(
                            "s" * 7_750 + warning,
                            review_marker="review",
                            index_marker="index",
                            eligibility_marker="eligibility",
                        )
                        if warning:
                            self.assertGreaterEqual(len(rendered.encode()), 10_000)
                        else:
                            self.assertLess(len(rendered.encode()), 10_000)
                        changed = (
                            *requests[:-1],
                            (
                                requests[-1][0],
                                requests[-1][1],
                                {**requests[-1][2], "body": rendered},
                                None,
                            ),
                        )
                        variants, _ = price_prefixes(journal, changed, native_calls=4)
                        commitment = _unpack_journal(variants[2])["effects"][-1][
                            "request"
                        ]["body"]
                        self.assertEqual(
                            commitment["utf8_bytes"], MAX_PUBLISHED_REVIEW_BODY_BYTES
                        )
                        self.assertGreaterEqual(
                            commitment["utf8_bytes"], len(rendered.encode())
                        )
                        self.assertEqual(
                            commitment["$body_sha256"],
                            hashlib.sha256(rendered.encode()).hexdigest(),
                        )
                        self.assertEqual(changed[-1][2]["body"], rendered)
                    self.assertEqual(len(documents), 5)
                    self.assertEqual(count, len(effects) + 3)
                    self.assertEqual(journal.effects, effects)
                    self.assertEqual(canonical_bytes(journal._record.to_dict()), record)
                    self.assertEqual(fixture.trace, trace)
                    self.assertEqual(budget.ordinary_remaining(), remaining)
                    observed.append(documents)
                return original(publisher, observer, journal, **kwargs)

            with patch.object(ordinary_publication_plan, "admit_current", inspect):
                self.assertEqual(fixture.invoke(), 0, fixture.error)
            self.assertTrue(observed)
