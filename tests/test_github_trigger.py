import unittest

from review_sensei.hosting.github.trigger import (
    choose_head_sha,
    extract_requested_commit,
    issue_comment_requests_rescan,
    resolve_issue_comment,
    resolve_pull_request_event,
    resolve_review_comment_event,
)


def _pull(*, head_sha: str = "b" * 40, base_sha: str = "a" * 40) -> dict[str, object]:
    return {
        "title": "Add provider profiles",
        "head": {"sha": head_sha, "ref": "feature/providers"},
        "base": {"sha": base_sha, "ref": "main"},
    }


class GitHubTriggerTests(unittest.TestCase):
    def test_issue_comment_requests_rescan(self):
        self.assertTrue(
            issue_comment_requests_rescan("@sensei please re-scan commit 016017b")
        )
        self.assertTrue(issue_comment_requests_rescan("@sensei re scan this PR"))
        self.assertFalse(issue_comment_requests_rescan("@sensei what changed?"))
        self.assertFalse(issue_comment_requests_rescan("please re-scan"))

    def test_extract_requested_commit(self):
        self.assertEqual(
            extract_requested_commit("@sensei please re-scan commit 016017b"),
            "016017b",
        )
        self.assertEqual(
            extract_requested_commit(f"@sensei re-scan {('c' * 40)}"),
            "c" * 40,
        )
        self.assertIsNone(extract_requested_commit("@sensei what changed?"))

    def test_choose_head_sha_accepts_prefix_or_exact_match(self):
        pull = _pull()
        self.assertEqual(choose_head_sha(pull, None), "b" * 40)
        self.assertEqual(choose_head_sha(pull, "bbbbbbb"), "b" * 40)
        self.assertEqual(choose_head_sha(pull, "b" * 40), "b" * 40)
        with self.assertRaisesRegex(ValueError, "does not match"):
            choose_head_sha(pull, "c" * 40)

    def test_resolve_issue_comment_rescan_routes_to_review(self):
        resolution = resolve_issue_comment(
            "@sensei please re-scan commit 016017b",
            _pull(head_sha="016017b" + ("0" * 33)),
        )
        self.assertEqual(resolution.operation, "review")
        self.assertEqual(resolution.enable_review, "true")
        self.assertEqual(resolution.head_sha, "016017b" + ("0" * 33))

    def test_resolve_issue_comment_mention_routes_to_reply(self):
        resolution = resolve_issue_comment(
            "@sensei what changed in the provider defaults?",
            _pull(),
        )
        self.assertEqual(resolution.operation, "reply")
        self.assertEqual(resolution.enable_review, "false")
        self.assertEqual(resolution.head_sha, "b" * 40)

    def test_resolve_pull_request_event(self):
        resolution = resolve_pull_request_event(_pull(), auto_review="true")
        self.assertEqual(resolution.operation, "review")
        self.assertEqual(resolution.enable_review, "true")

    def test_resolve_review_comment_event(self):
        resolution = resolve_review_comment_event("@sensei fixed?", _pull())
        self.assertEqual(resolution.operation, "reply")
        self.assertEqual(resolution.head_sha, "b" * 40)


if __name__ == "__main__":
    unittest.main()
