"""The retired-variable cleanup is exactly the set the product still reports.

The reusable workflow reports every retired managed repository variable it
still sees; the installation guide tells an operator which deletions are
authorized.  These checks bind the three artifacts together, so the documented
cleanup can neither delete a variable the product still reads nor omit one it
reports, and so a credential is never presented as a deletion target.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from review_sensei.configuration import (
    RETIRED_BEHAVIOR_ENVIRONMENT_SETTINGS,
    RETIRED_ENVIRONMENT_SETTINGS,
)

ROOT = Path(__file__).resolve().parents[1]
INSTALLATION = ROOT / "docs" / "installation.md"
RUNNER = ROOT / ".github" / "workflows" / "review-sensei-run.yml"

SECTION_TITLE = "### Retiring old repository variables"
REPORT_STEP = "Report retired repository variables"
PLAN_STEP = "Resolve the hosted plan"
SUPPORTED_OVERRIDES = ("REVIEWSENSEI_PROVIDER", "REVIEWSENSEI_MODEL")
CREDENTIALS = ("OLLAMA_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY")

VARIABLE_REFERENCE = re.compile(r"vars\.([A-Z0-9_]+)")
DELETE_COMMAND = re.compile(r"^gh variable delete ([A-Z0-9_]+)$")
TABLE_ROW = re.compile(r"^\| `([A-Z0-9_]+)` \| (.*) \|$")


def _step_block(text: str, name: str) -> str:
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.strip() == f"- name: {name}":
            start = index
            break
    if start is None:
        raise AssertionError(f"workflow step not found: {name}")
    end = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if re.match(r"^  \S", line) or line.startswith("      - name: "):
            end = index
            break
    return "\n".join(lines[start:end])


def _reported_variables() -> set[str]:
    block = _step_block(RUNNER.read_text(encoding="utf-8"), REPORT_STEP)
    return set(VARIABLE_REFERENCE.findall(block))


def _plan_overrides() -> set[str]:
    block = _step_block(RUNNER.read_text(encoding="utf-8"), PLAN_STEP)
    return set(VARIABLE_REFERENCE.findall(block))


def _installation_section() -> str:
    lines = INSTALLATION.read_text(encoding="utf-8").splitlines()
    start = lines.index(SECTION_TITLE)
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if re.match(r"^#{1,3} ", lines[index]):
            end = index
            break
    return "\n".join(lines[start:end])


def _documented_table() -> dict[str, str]:
    rows: dict[str, str] = {}
    for line in _installation_section().splitlines():
        match = TABLE_ROW.match(line)
        if match is not None:
            rows[match.group(1)] = match.group(2).strip()
    return rows


def _documented_fence(tag: str) -> str:
    bodies: list[str] = []
    lines = _installation_section().splitlines()
    index = 0
    while index < len(lines):
        if lines[index].strip() != f"```{tag}":
            index += 1
            continue
        index += 1
        while index < len(lines) and lines[index].strip() != "```":
            bodies.append(lines[index])
            index += 1
        index += 1
    if not bodies:
        raise AssertionError(f"no {tag} block in the cleanup section")
    return "\n".join(bodies)


def _documented_deletions() -> list[str]:
    return [
        match.group(1)
        for match in (
            DELETE_COMMAND.match(line)
            for line in _documented_fence("bash").splitlines()
        )
        if match is not None
    ]


def _normalized(text: str) -> str:
    return " ".join(text.replace("`", "").split())


class RetiredVariableCleanupTests(unittest.TestCase):
    def test_documented_cleanup_deletes_exactly_the_reported_variables(self):
        reported = _reported_variables()
        deleted = _documented_deletions()
        self.assertTrue(reported)
        # Every line in the cleanup block is one bounded deletion command, so
        # no other command can hide there.
        self.assertEqual(
            len(deleted),
            len(
                [
                    line
                    for line in _documented_fence("bash").splitlines()
                    if line.strip()
                ]
            ),
        )
        self.assertEqual(sorted(deleted), sorted(reported))
        self.assertEqual(len(set(deleted)), len(deleted))

    def test_documented_replacements_match_the_reported_remedies(self):
        from review_sensei.configuration import retired_environment_remedies

        remedies = dict(
            retired_environment_remedies(
                {name: "set" for name in _reported_variables()}
            )
        )
        table = _documented_table()
        self.assertEqual(sorted(table), sorted(remedies))
        for name, remedy in remedies.items():
            with self.subTest(variable=name):
                self.assertEqual(_normalized(table[name]), _normalized(remedy))

    def test_every_reported_variable_is_a_declared_retired_setting(self):
        declared = set(RETIRED_ENVIRONMENT_SETTINGS) | set(
            RETIRED_BEHAVIOR_ENVIRONMENT_SETTINGS
        )
        for name in sorted(_reported_variables()):
            with self.subTest(variable=name):
                self.assertIn(name, declared)

    def test_cleanup_excludes_supported_overrides_and_credentials(self):
        section = _installation_section()
        overrides = _plan_overrides()
        self.assertEqual(overrides, set(SUPPORTED_OVERRIDES))
        for name in SUPPORTED_OVERRIDES:
            with self.subTest(variable=name):
                self.assertNotIn(name, _documented_deletions())
                self.assertIn(name, section)
        self.assertNotIn("secret", _documented_fence("bash"))
        for credential in CREDENTIALS:
            with self.subTest(credential=credential):
                self.assertIn(credential, section)

    def test_documented_workflow_still_reads_only_the_supported_overrides(self):
        workflow = RUNNER.read_text(encoding="utf-8")
        self.assertEqual(
            set(VARIABLE_REFERENCE.findall(workflow)),
            set(SUPPORTED_OVERRIDES) | _reported_variables(),
        )


if __name__ == "__main__":
    unittest.main()
