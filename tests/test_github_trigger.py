import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from review_sensei.hosting.github.setup import _tagged_workflow
from review_sensei.hosting.github.trigger import (
    TriggerResolution,
    choose_head_sha,
    comment_mentions_sensei,
    extract_requested_commit,
    issue_comment_requests_rescan,
    resolve_issue_comment,
    resolve_pull_request_event,
    resolve_review_comment_event,
    write_github_output,
)
from review_sensei.hosting.github.trigger import (
    main as trigger_main,
)

ROOT = Path(__file__).resolve().parents[1]
REPO_CALLER = ROOT / ".github" / "workflows" / "review-sensei-review.yml"
EXAMPLE_CALLER = ROOT / "examples" / "github-actions" / "review-sensei-review.yml"
INLINE_RESOLVER_START = (
    'python - "$pull_json" "$AUTO_REVIEW" "$EVENT_NAME" "$COMMENT_BODY" <<\'PY\'\n'
)
INLINE_RESOLVER_END = "\n          PY\n"


def _pull(*, head_sha: str = "b" * 40, base_sha: str = "a" * 40) -> dict[str, object]:
    return {
        "number": 7,
        "title": "Add provider profiles",
        "head": {"sha": head_sha, "ref": "feature/providers"},
        "base": {"sha": base_sha, "ref": "main"},
    }


def _resolution(**overrides: str) -> TriggerResolution:
    values = {
        "operation": "reply",
        "head_sha": "b" * 40,
        "head_ref": "feature/providers",
        "base_ref": "main",
        "base_sha": "a" * 40,
        "pull_request_number": "7",
        "pull_request_title": "Add provider profiles",
        "enable_review": "false",
    }
    values.update(overrides)
    return TriggerResolution(**values)


def inline_resolver_script(text: str) -> str:
    start = text.find(INLINE_RESOLVER_START)
    if start < 0:
        raise AssertionError("caller is missing the inline trigger resolver")
    start += len(INLINE_RESOLVER_START)
    end = text.find(INLINE_RESOLVER_END, start)
    if end < 0:
        raise AssertionError("inline trigger resolver terminator is missing")
    return textwrap.dedent(text[start:end] + "\n")


class GitHubTriggerTests(unittest.TestCase):
    def test_issue_comment_requests_rescan(self):
        self.assertTrue(
            issue_comment_requests_rescan("@sensei please re-scan commit 016017b")
        )
        self.assertTrue(issue_comment_requests_rescan("@sensei re scan this PR"))
        self.assertTrue(issue_comment_requests_rescan("@sensei Re-Scan"))
        self.assertFalse(issue_comment_requests_rescan("@sensei what changed?"))
        self.assertFalse(issue_comment_requests_rescan("please re-scan"))
        self.assertFalse(issue_comment_requests_rescan("@SENSEI please re-scan"))
        self.assertFalse(issue_comment_requests_rescan("@sensei I rescanned the diff"))
        self.assertFalse(issue_comment_requests_rescan("@sensei rescanning now"))

    def test_maintainer_commands_route_to_the_command_operation(self):
        resolution = resolve_issue_comment("@sensei review pause", _pull())
        self.assertEqual(resolution.operation, "command")
        self.assertEqual(resolution.enable_review, "false")

    def test_oversized_maintainer_command_does_not_reach_command_execution(self):
        resolution = resolve_issue_comment(
            "@sensei review pause " + ("x" * 4096), _pull()
        )
        self.assertEqual(resolution.operation, "reply")

    def test_multibyte_padding_past_the_byte_bound_stays_out_of_command_execution(
        self,
    ):
        # Ideographic spaces are whitespace to str.strip() at three bytes each,
        # so this body stays command-shaped while passing 4096 bytes. A
        # character bound would route it to command instead of reply.
        body = "@sensei" + "\N{IDEOGRAPHIC SPACE}" * 2000 + "review pause"
        self.assertEqual(len(body), 2019)
        self.assertEqual(len(body.encode("utf-8")), 6019)
        self.assertEqual(resolve_issue_comment(body, _pull()).operation, "reply")

    def test_multibyte_command_reason_is_bounded_in_bytes(self):
        reason = "\N{LATIN SMALL LETTER E WITH ACUTE}" * 300
        self.assertLessEqual(len(reason), 512)
        self.assertGreater(len(reason.encode("utf-8")), 512)
        resolution = resolve_issue_comment(
            "@sensei dismiss " + "a" * 20 + " --reason " + reason, _pull()
        )
        self.assertEqual(resolution.operation, "reply")

    def test_command_body_byte_boundary_is_inclusive_at_4096(self):
        # 1359 ideographic spaces are 4077 bytes and are stripped by str.strip(),
        # so the remainder still parses as a command. "@sensei"+"review pause"
        # is 19 bytes, putting the body exactly on the 4096-byte ceiling.
        padding = "\N{IDEOGRAPHIC SPACE}" * 1359
        at_bound = "@sensei" + padding + "review pause"
        self.assertEqual(len(at_bound.encode("utf-8")), 4096)
        self.assertEqual(resolve_issue_comment(at_bound, _pull()).operation, "command")

        # One extra ASCII space pushes the body to 4097 bytes; it is also
        # stripped, so only the byte ceiling can route this one to reply.
        over_bound = "@sensei" + padding + " " + "review pause"
        self.assertEqual(len(over_bound.encode("utf-8")), 4097)
        self.assertEqual(resolve_issue_comment(over_bound, _pull()).operation, "reply")

    def test_comment_mentions_sensei_matches_workflow_gate(self):
        self.assertTrue(comment_mentions_sensei("@sensei please re-scan"))
        self.assertFalse(comment_mentions_sensei("@SENSEI please re-scan"))

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
        with self.assertRaisesRegex(ValueError, "does not match") as mismatch:
            choose_head_sha(pull, "c" * 40)
        self.assertNotIn("c" * 40, str(mismatch.exception))

    def test_resolve_rejects_double_dot_git_refs(self):
        pull = _pull()
        pull["head"] = {"sha": "b" * 40, "ref": "feature/../main"}
        with self.assertRaisesRegex(ValueError, "identity metadata is invalid"):
            resolve_pull_request_event(pull, auto_review="true")
        pull = _pull()
        pull["head"] = {"sha": "b" * 40, "ref": "feature/trailing/"}
        with self.assertRaisesRegex(ValueError, "identity metadata is invalid"):
            resolve_pull_request_event(pull, auto_review="true")

    def test_resolve_rejects_unsafe_git_refs(self):
        pull = _pull()
        pull["head"] = {"sha": "b" * 40, "ref": "feature/$(whoami)"}
        with self.assertRaisesRegex(ValueError, "identity metadata is invalid"):
            resolve_pull_request_event(pull, auto_review="true")

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
        self.assertEqual(resolution.pull_request_number, "7")

    def test_resolve_pull_request_event_normalizes_enable_review(self):
        resolution = resolve_pull_request_event(_pull(), auto_review="TRUE")
        self.assertEqual(resolution.enable_review, "false")

    def test_resolve_requires_pull_request_number(self):
        pull = _pull()
        pull["number"] = 0
        with self.assertRaisesRegex(ValueError, "pull request number is invalid"):
            resolve_pull_request_event(pull, auto_review="true")

    def test_resolve_review_comment_event(self):
        resolution = resolve_review_comment_event("@sensei fixed?", _pull())
        self.assertEqual(resolution.operation, "reply")
        self.assertEqual(resolution.head_sha, "b" * 40)
        self.assertEqual(resolution.pull_request_number, "7")

    def test_resolve_issue_comment_collapses_multiline_title(self):
        pull = _pull()
        pull["title"] = "Add provider\nprofiles"
        resolution = resolve_issue_comment("@sensei what changed?", pull)
        self.assertEqual(resolution.pull_request_title, "Add provider profiles")

    def test_resolve_issue_comment_collapses_unicode_line_separator_title(self):
        pull = _pull()
        pull["title"] = "Add provider\u2028profiles"
        resolution = resolve_issue_comment("@sensei what changed?", pull)
        self.assertEqual(resolution.pull_request_title, "Add provider profiles")

    def test_write_github_output_uses_heredoc_for_title(self):
        resolution = _resolution()
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "github-output"
            with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": str(output)}):
                write_github_output(resolution)
            text = output.read_text(encoding="utf-8")
        self.assertIn("operation=reply\n", text)
        self.assertIn("pull_request_number=7\n", text)
        self.assertIn("pull_request_title<<RS_PULL_REQUEST_TITLE\n", text)
        self.assertIn("Add provider profiles\nRS_PULL_REQUEST_TITLE\n", text)
        self.assertNotIn("pull_request_title=Add provider profiles\n", text)

    def test_write_github_output_rotates_title_delimiter(self):
        resolution = _resolution(pull_request_title="RS_PULL_REQUEST_TITLE in title")
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "github-output"
            with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": str(output)}):
                write_github_output(resolution)
            text = output.read_text(encoding="utf-8")
        self.assertIn("pull_request_title<<RS_PULL_REQUEST_TITLE_EOF\n", text)
        self.assertIn(
            "RS_PULL_REQUEST_TITLE in title\nRS_PULL_REQUEST_TITLE_EOF\n", text
        )

    def test_write_github_output_escapes_newline_title(self):
        resolution = _resolution(
            operation="review",
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
        resolution = _resolution()
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "GITHUB_OUTPUT is unavailable"):
                write_github_output(resolution)


class InlineCallerResolverTests(unittest.TestCase):
    def test_generated_callers_run_trigger_as_a_standalone_script(self):
        expected = 'PYTHONPATH=src python "$resolver"'
        for name, text in (
            ("repository", REPO_CALLER.read_text(encoding="utf-8")),
            ("example", EXAMPLE_CALLER.read_text(encoding="utf-8")),
            ("generated", _tagged_workflow("v5")),
        ):
            with self.subTest(name=name):
                self.assertIn(expected, text)
                self.assertNotIn(
                    "PYTHONPATH=src python -m review_sensei.hosting.github.trigger",
                    text,
                )

    def test_standalone_trigger_script_does_not_require_package_dependencies(self):
        resolver = ROOT / "src" / "review_sensei" / "hosting" / "github" / "trigger.py"
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(ROOT / "src")
        result = subprocess.run(
            [sys.executable, str(resolver), "--help"],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Resolve ReviewSensei workflow trigger metadata", result.stdout)

    def test_generated_callers_embed_the_same_inline_resolver(self):
        repo = REPO_CALLER.read_text(encoding="utf-8")
        example = EXAMPLE_CALLER.read_text(encoding="utf-8")
        generated = _tagged_workflow("v5")
        self.assertEqual(inline_resolver_script(repo), inline_resolver_script(example))
        self.assertEqual(
            inline_resolver_script(repo), inline_resolver_script(generated)
        )

    def test_inline_rescan_regex_matches_trigger_module(self):
        trigger = (
            ROOT / "src" / "review_sensei" / "hosting" / "github" / "trigger.py"
        ).read_text(encoding="utf-8")
        script = inline_resolver_script(REPO_CALLER.read_text(encoding="utf-8"))
        pattern = 're.compile(r"\\bre[\\s-]?scan\\b", re.IGNORECASE)'
        self.assertIn("_RESCAN = " + pattern, trigger)
        self.assertIn("rescan = " + pattern, script)

    def test_inline_fallback_matches_trigger_module_outputs(self):
        script = inline_resolver_script(REPO_CALLER.read_text(encoding="utf-8"))
        pull = _pull(head_sha="016017b" + ("0" * 33))
        cases = (
            ("issue_comment", "@sensei please re-scan commit 016017b", "false"),
            ("issue_comment", "@sensei what changed?", "false"),
            ("pull_request", "", "true"),
            ("pull_request_review_comment", "@sensei fixed?", "false"),
            ("workflow_dispatch", "", "false"),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pull_path = root / "pull.json"
            pull_path.write_text(json.dumps(pull), encoding="utf-8")
            for event, body, auto_review in cases:
                with self.subTest(event=event, body=body):
                    module_output = root / f"{event}-module.out"
                    inline_output = root / f"{event}-inline.out"
                    with mock.patch.dict(
                        os.environ, {"GITHUB_OUTPUT": str(module_output)}
                    ):
                        status = trigger_main(
                            [
                                "--event",
                                event,
                                "--comment-body",
                                body,
                                "--pull-json",
                                str(pull_path),
                                "--auto-review",
                                auto_review,
                            ]
                        )
                    self.assertEqual(status, 0)
                    result = subprocess.run(
                        [
                            sys.executable,
                            "-",
                            str(pull_path),
                            auto_review,
                            event,
                            body,
                        ],
                        input=script,
                        capture_output=True,
                        text=True,
                        check=False,
                        env={**os.environ, "GITHUB_OUTPUT": str(inline_output)},
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(
                        module_output.read_text(encoding="utf-8"),
                        inline_output.read_text(encoding="utf-8"),
                    )


if __name__ == "__main__":
    unittest.main()
