"""Capacity-only root commitments cover actual warning decimal widths."""

import hashlib
import unittest

from review_sensei_github import ordinary_publication_plan


class OrdinaryRootBodyCapacityTests(unittest.TestCase):
    def test_capacity_root_byte_width_covers_actual_warning_boundary(self):
        from review_sensei_github.publication import (
            MAX_PUBLISHED_REVIEW_BODY_BYTES,
            _framed_review_body,
            render_check_warning,
        )

        sizes = []
        for warning in ("", render_check_warning("check_permission", concise=True)):
            body = _framed_review_body(
                "s" * 7_750 + warning,
                review_marker="review",
                index_marker="index",
                eligibility_marker="eligibility",
            )
            request = {"body": body, "event": "COMMENT"}
            compact = ordinary_publication_plan._capacity_request_identity(
                "POST", "/repos/owner/repo/pulls/7/reviews", request
            )
            sizes.append(len(body.encode()))
            self.assertEqual(
                compact["body"]["utf8_bytes"], MAX_PUBLISHED_REVIEW_BODY_BYTES
            )
            self.assertEqual(
                compact["body"]["$body_sha256"],
                hashlib.sha256(body.encode()).hexdigest(),
            )
            self.assertEqual(compact["event"], "COMMENT")
            self.assertEqual(request["body"], body)
        self.assertLess(sizes[0], 10_000)
        self.assertGreaterEqual(sizes[1], 10_000)
