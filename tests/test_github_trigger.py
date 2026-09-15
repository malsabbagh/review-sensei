import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from review_sensei.hosting.github.trigger import (
    TriggerResolution,
    choose_head_sha,
    extract_requested_commit,
    issue_comment_requests_rescan,
    resolve_issue_comment,
    resolve_pull_request_event,
    resolve_review_comment_event,
    write_github_output,
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
        self.assertFalse(issue_comment_requests_rescan("@SENSEI please re-scan"))

    def test_extract_requested_commit(self):
        self.assertEqual(
            extract_requested_commit("@sensei please re-scan commit 016017b"),
            "016017b",
        )
        self.assertEqual(
            extract_requested_commit(f"@sensei re-scan commit {('c' * 40)}"),
            "c" * 40,
        )
        self.assertIsNone(extract_requested_commit("@sensei what changed?"))
        self.assertIsNone(
            extract_requested_commit("@sensei please re-scan issue 1234567")
        )
        self.assertIsNone(
            extract_requested_commit(
                "@sensei please re-scan this PR. It contains the hash 1234567."
            )
        )
        self.assertIsNone(extract_requested_commit(f"@sensei re-scan {('c' * 40)}"))
        self.assertIsNone(extract_requested_commit(None))  # type: ignore[arg-type]

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

    def test_resolve_issue_comment_collapses_multiline_title(self):
        pull = _pull()
        pull["title"] = "Add provider\nprofiles"
        resolution = resolve_issue_comment("@sensei what changed?", pull)
        self.assertEqual(resolution.pull_request_title, "Add provider profiles")

    def test_write_github_output_uses_heredoc_for_title(self):
        resolution = TriggerResolution(
            operation="reply",
            head_sha="b" * 40,
            head_ref="feature/providers",
            base_ref="main",
            base_sha="a" * 40,
            pull_request_title="Add provider profiles",
            enable_review="false",
        )
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "github-output"
            with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": str(output)}):
                write_github_output(resolution)
            text = output.read_text(encoding="utf-8")
        self.assertIn("operation=reply\n", text)
        self.assertIn("pull_request_title<<RS_PULL_REQUEST_TITLE\n", text)
        self.assertIn("Add provider profiles\nRS_PULL_REQUEST_TITLE\n", text)

    def test_write_github_output_escapes_newline_title(self):
        resolution = TriggerResolution(
            operation="review",
            head_sha="b" * 40,
            head_ref="feature/providers",
            base_ref="main",
            base_sha="a" * 40,
            pull_request_title="Add provider\nprofiles",
            enable_review="true",
        )
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "github-output"
            with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": str(output)}):
                write_github_output(resolution)
            text = output.read_text(encoding="utf-8")
        self.assertIn("pull_request_title<<RS_PULL_REQUEST_TITLE\n", text)
        self.assertIn("Add provider\nprofiles\nRS_PULL_REQUEST_TITLE\n", text)
        self.assertNotIn("pull_request_title=Add provider\n", text)

    def test_write_github_output_requires_github_output(self):
        resolution = TriggerResolution(
            operation="reply",
            head_sha="b" * 40,
            head_ref="feature/providers",
            base_ref="main",
            base_sha="a" * 40,
            pull_request_title="Add provider profiles",
            enable_review="false",
        )
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "GITHUB_OUTPUT is unavailable"):
                write_github_output(resolution)


if __name__ == "__main__":
    unittest.main()
