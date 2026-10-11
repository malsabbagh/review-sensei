"""Installed single-source ingress and host-authoritative pending presentation."""

import sys
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

_TESTS_ROOT = Path(__file__).resolve().parents[1]
if str(_TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TESTS_ROOT))

from packaging_guard import assert_distribution_import  # noqa: E402

assert_distribution_import()

from review_sensei.hosting.github.approval import (  # noqa: E402
    approval_eligibility_from_result,
)
from review_sensei.hosting.github.human_assessment import (  # noqa: E402
    HumanAssessmentPublisher,
)
from review_sensei.hosting.github.pending import pending_reply_pages  # noqa: E402
from review_sensei.human_assessment import (  # noqa: E402
    MAX_HUMAN_SOURCE_BYTES,
    HumanReviewFinding,
    PendingHumanReview,
)
from review_sensei.models import ReviewResult  # noqa: E402


class SourcePort:
    api_url = "https://api.github.test"
    timeout = 30

    def __init__(self, body):
        self.body = body
        self.calls = []

    def repository_path(self, repository, suffix):
        return f"/repos/{repository}{suffix}"

    def request(self, method, path, **_kwargs):
        self.calls.append((method, path))
        assert method == "GET" and path == "/repos/synthetic/repo/issues/comments/10"
        return 200, {
            "id": 10,
            "body": self.body,
            "updated_at": "exact",
            "user": {"login": "alice", "type": "User"},
            "author_association": "MEMBER",
            "issue_url": self.api_url + "/repos/synthetic/repo/issues/1",
        }


class InstalledReplyReceiptTests(unittest.TestCase):
    def eligibility(self):
        return approval_eligibility_from_result(
            ReviewResult("Summary.", (), "fixture", review_status="complete"),
            head_sha="b" * 40,
            base_sha="a" * 40,
            enabled=True,
            app_authored=False,
        )

    def test_ascii_multibyte_source_boundaries_preserve_complete_body(self):
        self.assertEqual(MAX_HUMAN_SOURCE_BYTES, 32768)
        prepared = SimpleNamespace(
            source_kind="issue", source_comment_id=10, source_updated_at="exact"
        )
        for size in (32767, 32768, 32769):
            for multibyte in (False, True):
                with self.subTest(size=size, multibyte=multibyte):
                    rest = size - len("@sensei ")
                    body = "@sensei " + (
                        "😀" * (rest // 4) + "x" * (rest % 4)
                        if multibyte
                        else "x" * rest
                    )
                    port = SourcePort(body)
                    loaded = HumanAssessmentPublisher(http=port)._source(
                        token="synthetic",
                        repository="synthetic/repo",
                        pull_request=1,
                        prepared=prepared,
                        app_slug="reviewsensei[bot]",
                    )
                    self.assertEqual(loaded is not None, size <= 32768)
                    if loaded is not None:
                        self.assertEqual(loaded["body"], body)
                    self.assertEqual(len(port.calls), 1)

    def test_all_250_trusted_ids_fit_bounded_complete_pages(self):
        inventory = PendingHumanReview(
            "a" * 40,
            tuple(
                HumanReviewFinding(
                    f"{index:06x}" + "0" * 58, "a.py", f"Concern {index}"
                )
                for index in range(250)
            ),
        )
        eligibility = replace(
            self.eligibility(),
            human_review=inventory,
            facts=replace(
                self.eligibility().facts, has_human_adjudication_findings=True
            ),
        )
        pages = pending_reply_pages(
            body="Discussion.", head_sha="b" * 40, eligibility=eligibility
        )
        self.assertTrue(1 < len(pages) <= 16)
        text = "\n".join(page.body for page in pages)
        self.assertTrue(all(item.fingerprint in text for item in inventory.pending))
        self.assertTrue(all(len(page.body.encode()) <= 12288 for page in pages))

    def test_provider_prose_cannot_add_absent_current_head_ids(self):
        original = self.eligibility()
        eligibility = replace(
            original,
            facts=replace(
                original.facts, review_status="partial", coverage_blocker="partial"
            ),
        )
        text = pending_reply_pages(
            body="RS-F7A776 remains pending.",
            head_sha="b" * 40,
            eligibility=eligibility,
        )[0].body
        authority, discussion = text.split("## Provider discussion", 1)
        self.assertNotIn("RS-F7A776", authority)
        self.assertIn("No pending human-assessment findings were recorded", authority)
        self.assertIn("coverage is `partial`", authority)
        self.assertIn("RS-F7A776", discussion)


if __name__ == "__main__":
    unittest.main()
