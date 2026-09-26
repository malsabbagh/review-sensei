"""Executable checks that current instructions match the installed artifacts.

Slice G requires that the actual README, site, config, and help snippets
execute against the selected installed artifacts, so this module does not
inspect documentation as prose:

* configuration snippets are loaded by the packaged configuration loader, so a
  documentation-only field or a snippet that is not a valid document fails;
* documented command lines are fed to the packaged CLI parsers, so a renamed or
  removed flag fails;
* the offline, side-effect-free commands the documentation shows are really
  executed as subprocesses, so a changed exit contract fails.

No check performs a provider call, a network request, or a GitHub write.
"""

from __future__ import annotations

import io
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from html import unescape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

README = ROOT / "README.md"
INSTALLATION = ROOT / "docs" / "installation.md"
PUBLIC_CONTRACTS = ROOT / "docs" / "public-contracts.md"
GETTING_STARTED = ROOT / "docs" / "site" / "getting-started" / "index.html"

FENCE = re.compile(r"^```([A-Za-z0-9_-]*)\s*$")
COMMAND_BLOCK = re.compile(r'<pre class="command"[^>]*>(.*?)</pre>', re.DOTALL)
REVIEW_SENSEI_LINE = "review-sensei "

# Documented commands are parsed against the parser that owns their first
# token; the default (no subcommand) review command uses the main parser.
COMMAND_PARSERS = {
    "config": "_config_parser",
    "doctor": "_doctor_parser",
    "plan": "_plan_parser",
    "host-plan": "_host_plan_parser",
    "prepare-diff": "_prepare_diff_parser",
    "evaluate": "_evaluate_parser",
    "evaluate-convergence": "_evaluate_convergence_parser",
    "github": "_github_parser",
    "learnings": "_learnings_parser",
    "promotion": "_promotion_parser",
    "resolve-hosted-openrouter": "_resolve_hosted_openrouter_parser",
}

# Documented placeholders stand in for operator values; parsing never touches
# the path or file a snippet names.
PLACEHOLDERS = {
    "<base>": "a" * 40,
    "<head>": "b" * 40,
    "<trusted-base-commit-sha>": "c" * 40,
    "/path/to/target-branch": "/tmp/target-branch",
    "/path/to/target-branch-checkout": "/tmp/target-branch",
}


def _fenced_blocks(path: Path, tag: str):
    """Yield the bodies of fenced code blocks tagged ``tag``."""

    lines = path.read_text(encoding="utf-8").splitlines()
    index = 0
    while index < len(lines):
        match = FENCE.match(lines[index])
        if match is None or match.group(1) != tag:
            index += 1
            continue
        body: list[str] = []
        index += 1
        while index < len(lines) and FENCE.match(lines[index]) is None:
            body.append(lines[index])
            index += 1
        index += 1
        yield "\n".join(body)


def _command_blocks(path: Path):
    """Yield the bodies of the site page's copyable command blocks."""

    text = path.read_text(encoding="utf-8")
    for block in COMMAND_BLOCK.findall(text):
        yield unescape(block)


def _documented_commands(path: Path, blocks):
    """Join documented invocations, folding ``\\`` and flag continuations."""

    produced: list[str] = []
    current: str | None = None
    for block in blocks:
        for line in block.splitlines():
            stripped = line.strip()
            if stripped.startswith(REVIEW_SENSEI_LINE):
                if current:
                    produced.append(current)
                current = stripped
            elif current and (stripped.startswith("-") or current.endswith("\\")):
                current = current.rstrip("\\").rstrip() + " " + stripped
            else:
                if current:
                    produced.append(current)
                current = None
    if current:
        produced.append(current)
    return produced


def _documented_command_lines():
    """Every documented ``review-sensei`` invocation, by source document."""

    for path in (README, INSTALLATION, PUBLIC_CONTRACTS):
        for command in _documented_commands(path, _fenced_blocks(path, "bash")):
            yield path, command
    for command in _documented_commands(
        GETTING_STARTED, _command_blocks(GETTING_STARTED)
    ):
        yield GETTING_STARTED, command


def _configuration_snippets():
    """Yield (document path, snippet text) for every configuration snippet."""

    for path in (README, INSTALLATION):
        for body in _fenced_blocks(path, "yaml"):
            yield path, body
    for body in _command_blocks(GETTING_STARTED):
        marker = [
            index
            for index, line in enumerate(body.splitlines())
            if ".reviewsensei.yml" in line
        ]
        if not marker:
            continue
        snippet = "\n".join(body.splitlines()[marker[0] :])
        yield GETTING_STARTED, snippet


def _load_snippet(snippet: str):
    from review_sensei.configuration import parse_configuration_text

    document = snippet
    if not any(line.startswith("schema:") for line in document.splitlines()):
        document = "schema: 1\n" + document
    return parse_configuration_text(document, source="documented snippet")


def _installed_cli():
    from review_sensei import cli

    return cli


def _documented_command_environment() -> dict[str, str]:
    environment = dict(os.environ)
    source_root = str(ROOT / "src")
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        source_root if not existing else source_root + os.pathsep + existing
    )
    return environment


class DocumentedCommandParsingTests(unittest.TestCase):
    def test_documented_command_lines_parse_with_the_installed_cli(self) -> None:
        cli = _installed_cli()
        commands = list(_documented_command_lines())
        self.assertTrue(commands, "no documented command lines were extracted")
        for path, command in commands:
            with self.subTest(document=path.name, command=command):
                tokens = shlex.split(command)
                self.assertEqual(tokens[0], "review-sensei")
                self.assertGreater(len(tokens), 1, "snippet names no argument")
                for placeholder, value in PLACEHOLDERS.items():
                    tokens = [
                        value if token == placeholder else token for token in tokens
                    ]
                rest = tokens[1:]
                factory_name = COMMAND_PARSERS.get(rest[0])
                if factory_name is None:
                    parser = cli._parser()
                else:
                    parser = getattr(cli, factory_name)()
                    rest = rest[1:]
                # argparse exits 2 on an unknown flag and 0 for --help; both
                # are the parser's own verdict on the documented snippet.
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    try:
                        parser.parse_args(rest)
                    except SystemExit as exc:
                        self.assertEqual(exc.code, 0, f"parser rejected: {command}")

    def test_extraction_covers_the_documented_subcommands(self) -> None:
        commands = "\n".join(command for _, command in _documented_command_lines())
        for expected in (
            "review-sensei --version",
            "review-sensei --help",
            "review-sensei --diff ",
            "review-sensei doctor --json",
            "review-sensei prepare-diff ",
            "review-sensei plan ",
            "review-sensei learnings diagnose ",
            "review-sensei evaluate-convergence ",
            "review-sensei resolve-hosted-openrouter ",
        ):
            with self.subTest(snippet=expected):
                self.assertIn(expected, commands)


class DocumentedConfigurationSnippetTests(unittest.TestCase):
    def test_configuration_snippets_load_with_the_packaged_loader(self) -> None:
        snippets = list(_configuration_snippets())
        self.assertTrue(snippets, "no configuration snippets were extracted")
        for path, snippet in snippets:
            with self.subTest(document=path.name, snippet=snippet.splitlines()[0]):
                configuration = _load_snippet(snippet)
                self.assertEqual(configuration.schema, 1)
                for line in snippet.splitlines():
                    stripped = line.strip()
                    if stripped.startswith("backend:"):
                        declared = stripped.split(":", 1)[1].split("#", 1)[0].strip()
                        self.assertEqual(configuration.inference.backend, declared)
                    if stripped.startswith("reviews:"):
                        declared = stripped.split(":", 1)[1].split("#", 1)[0].strip()
                        self.assertEqual(configuration.github.reviews, declared)

    def test_configuration_snippet_assertions_are_exercised(self) -> None:
        """Guard the extractor: the checked snippets must carry real fields."""

        snippets = "\n".join(body for _, body in _configuration_snippets())
        for expected in ("schema:", "inference:", "backend:", "github:", "reviews:"):
            with self.subTest(field=expected):
                self.assertIn(expected, snippets)


class DocumentedCommandExecutionTests(unittest.TestCase):
    """Run the documented offline commands against the installed package."""

    def _run(self, directory: Path, *arguments: str):
        return subprocess.run(
            [sys.executable, "-m", "review_sensei", *arguments],
            cwd=directory,
            env=_documented_command_environment(),
            capture_output=True,
            text=True,
            timeout=120,
        )

    def test_documented_version_and_config_commands_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            version = self._run(directory, "--version")
            self.assertEqual(version.returncode, 0, version.stderr)
            self.assertRegex(version.stdout.strip(), r"^\d+\.\d+\.\d+$")
            # `review-sensei config validate` / `config show [--explain]` are
            # documented inline in docs/installation.md.
            for arguments in (("config", "validate"), ("config", "show", "--explain")):
                with self.subTest(command=" ".join(arguments)):
                    result = self._run(directory, *arguments)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("local-ollama", result.stdout)

    def test_documented_doctor_command_reports_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = self._run(Path(temporary), "doctor", "--json")
            # A bare directory legitimately reports `action` for the
            # automated-review admission check; both documented verdicts are
            # exit 0 (configured checks passed) and exit 2 (action required).
            self.assertIn(result.returncode, (0, 2), result.stderr)
            report = json.loads(result.stdout)
            self.assertIn(report.get("status"), ("pass", "action"))
            self.assertIn("checks", report)
            self.assertTrue(
                any(check.get("name") == "package" for check in report["checks"])
            )

    def test_documented_plan_command_runs_on_a_bounded_diff(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "pr.patch").write_text(
                "diff --git a/a.py b/a.py\n"
                "--- a/a.py\n"
                "+++ b/a.py\n"
                "@@ -0,0 +1,2 @@\n"
                "+print(1)\n"
                "+print(2)\n",
                encoding="utf-8",
            )
            result = self._run(
                directory,
                "plan",
                "--diff",
                "pr.patch",
                "--repository",
                "owner/repo",
                "--pull-request",
                "42",
                "--base-sha",
                "a" * 40,
                "--head-sha",
                "b" * 40,
                "--json",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            plan = json.loads(result.stdout)
            self.assertEqual(plan["identity"]["repository"], "owner/repo")

    def test_documented_prepare_diff_command_runs_in_a_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            tracked = directory / "tracked.txt"

            def git(*arguments: str) -> None:
                subprocess.run(
                    ["git", *arguments], cwd=directory, check=True, capture_output=True
                )

            git("init", "-q", "-b", "main")
            git("config", "user.email", "docs@example.invalid")
            git("config", "user.name", "Documentation Check")
            tracked.write_text("base\n", encoding="utf-8")
            git("add", "tracked.txt")
            git("commit", "-q", "-m", "base")
            git("checkout", "-q", "-b", "feature")
            tracked.write_text("head\n", encoding="utf-8")
            git("add", "tracked.txt")
            git("commit", "-q", "-m", "head")
            git("checkout", "-q", "main")
            result = self._run(
                directory,
                "prepare-diff",
                "--repository",
                ".",
                "--base-ref",
                "main",
                "--head-ref",
                "feature",
                "--output",
                "pr.patch",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            patch = (directory / "pr.patch").read_text(encoding="utf-8")
            self.assertIn("diff --git", patch)
            self.assertIn("+head", patch)


if __name__ == "__main__":
    unittest.main()
