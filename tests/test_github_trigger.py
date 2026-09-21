import ast
import itertools
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from review_sensei.disposition import parse_maintainer_command
from review_sensei.errors import ReviewInputError
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


INLINE_COMMAND_PREFILTER_START = (
    'len(comment_body.encode("utf-8")) <= 4096 and re.search(r"'
)


def inline_command_prefilter(text: str) -> re.Pattern[str]:
    """Return the inline command prefilter exactly as the heredoc evaluates it.

    The template stores the pattern in a raw Python string literal, so a
    doubled backslash is a literal backslash to the regex engine. Parsing the
    literal instead of retyping the grammar is what keeps this test honest.
    """

    start = text.find(INLINE_COMMAND_PREFILTER_START)
    if start < 0:
        raise AssertionError("caller is missing the inline command prefilter")
    start += len(INLINE_COMMAND_PREFILTER_START)
    end = text.find('"', start)
    if end < 0:
        raise AssertionError("inline command prefilter terminator is missing")
    literal = text[start:end]
    return re.compile(ast.literal_eval(f'r"{literal}"'), re.IGNORECASE)


def command_parity_cases() -> list[dict[str, object]]:
    fixture_path = ROOT / "tests" / "fixtures" / "maintainer-command-parity.json"
    return json.loads(fixture_path.read_text(encoding="utf-8"))


def parses_as_command(body: str) -> bool:
    """Return the authoritative parser's decision for a comment body.

    The parser refuses some bodies by raising, so an exception is a rejection
    rather than a test error.
    """

    try:
        return parse_maintainer_command(body, actor="test-maintainer") is not None
    except ReviewInputError:
        return False


def repaired_command(body: str) -> str:
    """Return a body with the documented over-acceptance dimensions removed.

    The prefilter may only be wider than the parser along three axes: the
    mention token's casing (the regex is case-insensitive, the parser's
    mention is not), `\\s` against the parser's ASCII separator bound, and the
    disposition reason value (emptiness, printability, the 512-byte bound).
    Lowercasing the mention, collapsing whitespace, and replacing the reason
    with a short printable one must therefore yield a body the parser accepts;
    a body that still fails is a shape no documented rule covers.
    """

    repaired = re.sub(r"(?i)@sensei", "@sensei", body)
    repaired = re.sub(r"\s+", " ", repaired)
    if parses_as_command(repaired):
        return repaired
    head, separator, _ = repaired.rpartition("--reason")
    if not separator:
        return repaired
    return f"{head}{separator} accepted"


def command_corpus() -> list[str]:
    """Enumerate command-shaped bodies from the grammar rather than by hand.

    The fixed fixture pins behavior for known shapes; this corpus spells the
    grammar's own tokens, separators, casing, and reason variants so a parser
    addition the prefilter cannot route fails here instead of falling through
    to a conversational reply at runtime.
    """

    fingerprint16 = "0123456789abcdef"
    fingerprint64 = "abcdef0123456789" * 4
    commands = (
        "review status",
        "review pause",
        "review continue",
        "review continue --rounds 0",
        "review continue --rounds 1",
        "review reenroll",
        "verify",
        f"dismiss {fingerprint16} --reason x",
        f"defer {fingerprint64} --reason x",
        f"accept-risk {fingerprint16} --reason x",
        f"dismiss {fingerprint16.upper()} --reason x",
        f"dismiss {fingerprint16[:-1]} --reason x",
        f"dismiss {fingerprint64}a --reason x",
    )
    reasons = (
        "x",
        "accepted reason",
        '"quoted reason"',
        '""',
        "\u2003",
        "é",
        "a" * 512,
        "a" * 513,
        "x\ny",
        "x --reason y",
    )
    bodies: list[str] = []
    for command, mention, prefix, separator, suffix in itertools.product(
        commands,
        ("@sensei", "@Sensei", "@SENSEI"),
        ("", "note ", "note\n", "> "),
        (" ", "\t", "\n", "  "),
        ("", " ", "\n", " trailing"),
    ):
        tokenized = re.sub(r"\s+", lambda _match: separator, command)
        bodies.append(f"{prefix}{mention}{separator}{tokenized}{suffix}")
    for reason, prefix, separator in itertools.product(
        reasons, ("", "note ", "x"), (" ", "\t", "\n", "  ")
    ):
        bodies.append(
            f"{prefix}@sensei{separator}dismiss{separator}{fingerprint16}"
            f"{separator}--reason{separator}{reason}"
        )
    return bodies


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

    def test_every_parser_accepted_command_routes_to_the_command_operation(self):
        # The prefilter is deliberately narrower work than the parser, so the
        # invariant is one-directional: anything the authoritative parser
        # accepts must reach the hosted command handler instead of falling
        # through to a conversational reply.
        cases = command_parity_cases()
        accepted = [case["body"] for case in cases if case["accepted"]]
        self.assertIn("@sensei review reenroll", accepted)
        self.assertGreater(len(accepted), 0)
        for body in accepted:
            with self.subTest(body=body):
                self.assertEqual(
                    resolve_issue_comment(body, _pull()).operation, "command"
                )

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

    def test_embedded_command_prefilter_covers_every_parser_accepted_command(self):
        prefilter = inline_command_prefilter(REPO_CALLER.read_text(encoding="utf-8"))
        for name, text in (
            ("example", EXAMPLE_CALLER.read_text(encoding="utf-8")),
            ("generated", _tagged_workflow("v5")),
        ):
            with self.subTest(name=name):
                self.assertEqual(
                    inline_command_prefilter(text).pattern, prefilter.pattern
                )
        for case in command_parity_cases():
            body = case["body"]
            with self.subTest(body=body):
                self.assertEqual(parses_as_command(body), case["accepted"])
        # The invariant that keeps the feature working: a command the
        # authoritative parser accepts must never fall through to a
        # conversational reply on a caller without the packaged module. The
        # accepted side is derived from the parser, so a grammar change that
        # the prefilter cannot route fails here instead of misrouting at
        # runtime.
        accepted = 0
        for body in command_corpus():
            if not parses_as_command(body):
                continue
            accepted += 1
            with self.subTest(body=body):
                self.assertTrue(prefilter.search(body) is not None)
        self.assertGreater(accepted, 200)
        # Over-acceptance is bounded by the documented dimensions rather than
        # by a hand-pinned list: every corpus body the prefilter matches while
        # the parser rejects must become parser-accepted once its whitespace is
        # ASCII and its reason is short and printable.
        over_accepted = [
            body
            for body in command_corpus()
            if not parses_as_command(body) and prefilter.search(body) is not None
        ]
        for body in over_accepted:
            with self.subTest(body=body):
                self.assertTrue(
                    parses_as_command(repaired_command(body)),
                    "prefilter matched a shape outside the documented "
                    "over-acceptance dimensions",
                )
        self.assertGreater(len(over_accepted), 0)
        # The prefilter is allowed to be wider than the parser because the
        # reusable workflow re-parses the body and fails closed on
        # "not-a-command". These eight fixture bodies differ on purpose: a
        # mention whose casing the case-insensitive regex accepts, reasons that
        # the parser bounds by emptiness, by printable ASCII, or by 512 bytes,
        # and separators that the parser restricts to ASCII whitespace while
        # the prefilter uses \s. Any other difference means the two grammars
        # drifted.
        fingerprint = "abcd1234abcd1234"
        self.assertEqual(
            {
                case["body"]
                for case in command_parity_cases()
                if not case["accepted"] and prefilter.search(case["body"]) is not None
            },
            {
                "@Sensei review pause",
                "@SENSEI review pause",
                f'@sensei dismiss {fingerprint} --reason ""',
                f'@sensei dismiss {fingerprint} --reason "accepted"\u2003',
                f"@sensei dismiss {fingerprint} --reason " + "x" * 513,
                "@sensei review\x1creenroll",
                "@sensei review\x85reenroll",
                "@sensei review\u2003reenroll",
            },
        )

    def test_inline_fallback_matches_trigger_module_outputs(self):
        script = inline_resolver_script(REPO_CALLER.read_text(encoding="utf-8"))
        pull = _pull(head_sha="016017b" + ("0" * 33))
        cases = (
            ("issue_comment", "@sensei please re-scan commit 016017b", "false"),
            ("issue_comment", "@sensei what changed?", "false"),
            ("issue_comment", "@sensei review status", "false"),
            ("issue_comment", "@sensei review pause", "false"),
            ("issue_comment", "@sensei review reenroll", "false"),
            ("issue_comment", "@sensei Review Reenroll", "false"),
            ("issue_comment", "@sensei review continue --rounds 0", "false"),
            ("issue_comment", "@sensei verify", "false"),
            (
                "issue_comment",
                "@sensei dismiss abcd1234abcd1234 --reason accepted",
                "false",
            ),
            ("issue_comment", "@sensei review continue --rounds 2", "false"),
            ("issue_comment", "@sensei review reenroll trailing", "false"),
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
