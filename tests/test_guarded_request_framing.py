"""Shared sizing frames preserve the actual review renderer's exact bytes."""

import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch

from review_sensei_github import ordinary_publication_plan as plan
from review_sensei_github import publication as pub

from tests import test_recovery_context_g17 as recovery


class GuardedRequestFramingTests(unittest.TestCase):
    def test_shared_request_frame_matches_original_renderer_without_io(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = (
                recovery.OrdinaryEntryHostedAndCliRecoveryG17Tests._make_hosted_fixture(
                    directory
                )
            )
            original = plan.current_request_branches
            seen = []

            def capture(publisher, journal, **kwargs):
                before = list(fixture.trace)
                result = original(publisher, journal, **kwargs)
                self.assertEqual(fixture.trace, before)
                eligibility = kwargs["eligibility"]
                assembled = kwargs["assembled"]
                pending = replace(
                    eligibility,
                    facts=replace(
                        eligibility.facts,
                        check_published=False,
                        has_blocking_findings=any(
                            e.blocking and e.body_placed
                            for e in assembled.index_entries
                        )
                        or kwargs.get("window_non_inline_blocking", False),
                    ),
                )
                marker = pub.approval_eligibility_marker(pending)
                summary, index = pub.fit_assembled_review(
                    assembled,
                    review_marker=kwargs["marker"],
                    eligibility_marker=marker,
                    repository_id=journal.identity.repository_id,
                    pull_request=journal.identity.pull_request,
                    head_sha=journal.head_sha,
                    base_sha=journal.base_sha,
                    result_digest=pub.review_result_digest(kwargs["result"]),
                    summary_limit=pub._published_summary_byte_limit(kwargs["result"]),
                    complete_inventory=kwargs["complete_inventory"],
                    reserved_bytes=pub._disposition_frame_reserve(
                        eligibility, assembled.index_entries
                    ),
                    preserve_explanations=kwargs.get("preserve_explanations", False),
                )
                expected = pub._framed_review_body(
                    summary,
                    review_marker=kwargs["marker"],
                    index_marker=index,
                    eligibility_marker=marker,
                )
                for branch in result:
                    for method, path, body, _ in branch:
                        if (
                            method == "POST"
                            and path.endswith("/reviews")
                            and body["event"] != "APPROVE"
                        ):
                            self.assertEqual(body["body"].encode(), expected.encode())
                            self.assertEqual(
                                body["comments"],
                                [
                                    publisher._inline_comment_payload(
                                        comment, prose, anchor
                                    )
                                    for comment, _fingerprint, prose, anchor in assembled.inline_comments
                                ],
                            )
                            seen.append(body["body"])
                return result

            with patch.object(plan, "current_request_branches", capture):
                self.assertEqual(fixture.invoke(), 0, fixture.error)
            self.assertTrue(seen)
