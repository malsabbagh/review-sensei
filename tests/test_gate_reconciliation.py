"""The gate reconciliation is bounded, and the App holds no dismissal power.

ADR 0057 replaces the persistent change request with the head-bound
`ReviewSensei` check, so an installation upgraded from an earlier release can
carry obsolete App-authored change requests and a required-check rule nothing
publishes any more.  The installation guide's "Reconciling a prior gate state"
section is the authorized operator transition for that state.  These checks
bind the section to the product: dismissal targets the App's own review through
the documented endpoint and nothing else, the required-check guidance covers
the rollback and writes-disabled states, the documented capability table, the
Worker's capability map, and the Python broker client are the same closed set
with no dismissal or administration permission anywhere, and the doctor gate
line matches the documented sample.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from review_sensei.diagnostics import run_doctor
from review_sensei.hosting.github.checks import REVIEW_CHECK_NAME
from review_sensei.hosting.github.setup import SETUP_APP_LOGIN

ROOT = Path(__file__).resolve().parents[1]
INSTALLATION = ROOT / "docs" / "installation.md"
APP_AUTH = ROOT / "docs" / "github-app-auth.md"
GATE_ADR = ROOT / "docs" / "adr" / "0057-one-check-run-as-the-single-merge-authority.md"
TOKEN_BROKER = ROOT / "deploy" / "cloudflare" / "src" / "token-broker.ts"
BROKER_CLIENT = (
    ROOT / "src" / "review_sensei" / "hosting" / "github" / "broker_client.py"
)

SECTION_TITLE = "## Reconciling a prior gate state"
CAPABILITY_HEADING = "### Issue-64 Worker capability broker"
DISMISSAL = re.compile(
    r'^gh api --method POST "repos/OWNER/REPO/pulls/NUMBER/reviews/'
    r'REVIEW_ID/dismissals" -f message="[^"]+"$'
)
TABLE_ROW = re.compile(r"^\| `([a-z_]+)` \| (.+) \|$")
BACKTICKED = re.compile(r"`([^`]+)`")
WORKER_CAPABILITY = re.compile(r"^\s{2}([a-z_]+): \{(.*)\},\s*$")
WORKER_PERMISSION = re.compile(r'"?([a-z_]+)"?: "([a-z]+)"')
STALE_CLAIM = "cannot read or change branch protection"


def _section(path: Path, title: str) -> str:
    lines = path.read_text(encoding="utf-8").splitlines()
    start = lines.index(title)
    for index in range(start + 1, len(lines)):
        if re.match(r"^#{1,2} ", lines[index]):
            return "\n".join(lines[start:index])
    return "\n".join(lines[start:])


def _reconciliation_section() -> str:
    return _section(INSTALLATION, SECTION_TITLE)


def _bash_lines(section: str) -> list[str]:
    lines = section.splitlines()
    commands: list[str] = []
    index = 0
    while index < len(lines):
        if lines[index].strip() != "```bash":
            index += 1
            continue
        index += 1
        while index < len(lines) and lines[index].strip() != "```":
            if lines[index].strip():
                commands.append(lines[index].strip())
            index += 1
        index += 1
    return commands


def _documented_capabilities() -> dict[str, set[str]]:
    lines = APP_AUTH.read_text(encoding="utf-8").splitlines()
    start = lines.index(CAPABILITY_HEADING)
    rows: dict[str, set[str]] = {}
    for line in lines[start:]:
        match = TABLE_ROW.match(line)
        if match is None and rows:
            break
        if match is not None:
            rows[match.group(1)] = set(BACKTICKED.findall(match.group(2)))
    return rows


def _worker_capabilities() -> dict[str, set[str]]:
    text = TOKEN_BROKER.read_text(encoding="utf-8")
    body = text.split("const CAPABILITIES = {", 1)[1].split("} as const;", 1)[0]
    rows: dict[str, set[str]] = {}
    for line in body.splitlines():
        match = WORKER_CAPABILITY.match(line)
        if match is None:
            continue
        rows[match.group(1)] = {
            f"{name}: {scope}"
            for name, scope in WORKER_PERMISSION.findall(match.group(2))
        }
    return rows


def _python_capabilities() -> set[str]:
    text = BROKER_CLIENT.read_text(encoding="utf-8")
    body = text.split("if capability not in {", 1)[1].split("}:", 1)[0]
    return set(re.findall(r'"([a-z_]+)"', body))


def _normalized(text: str) -> str:
    return " ".join(text.replace("`", "").split())


class GateReconciliationTests(unittest.TestCase):
    def test_reconciliation_dismisses_only_the_apps_own_stale_review(self):
        section = _reconciliation_section()
        commands = _bash_lines(section)
        self.assertTrue(commands)

        # Only bounded reads and the single dismissal endpoint appear, so no
        # other host mutation can hide in the section.
        dismissal = [line for line in commands if "dismissals" in line]
        self.assertEqual(len(dismissal), 1)
        self.assertRegex(dismissal[0], DISMISSAL)
        for line in commands:
            with self.subTest(command=line):
                self.assertTrue(line.startswith("gh "), line)
                if "dismissals" not in line:
                    self.assertNotIn("--method", line)

        discovery = [line for line in commands if line.startswith("gh pr list")]
        self.assertEqual(len(discovery), 1)
        self.assertIn("--state open", discovery[0])
        self.assertIn('reviewDecision == "CHANGES_REQUESTED"', discovery[0])

        reviews_read = [
            line
            for line in commands
            if line.startswith('gh api "repos/OWNER/REPO/pulls')
        ]
        self.assertEqual(len(reviews_read), 1)
        self.assertIn("/reviews", reviews_read[0])
        self.assertIn(".user.login", reviews_read[0])

        normalized = _normalized(section)
        self.assertIn(SETUP_APP_LOGIN, section)
        self.assertIn("Never dismiss a human review", normalized)
        self.assertIn("only a review authored by that login", normalized)
        self.assertIn("never emits REQUEST_CHANGES", normalized)
        self.assertIn(REVIEW_CHECK_NAME, section)

    def test_required_check_reconciliation_covers_rollback_and_writes_disabled(self):
        section = _reconciliation_section()
        commands = _bash_lines(section)
        normalized = _normalized(section)

        protection_read = [
            line for line in commands if "/branches/BRANCH/protection" in line
        ]
        rules_read = [line for line in commands if "/rules/branches/BRANCH" in line]
        self.assertEqual(len(protection_read), 1)
        self.assertEqual(len(rules_read), 1)
        for line in (*protection_read, *rules_read):
            with self.subTest(command=line):
                self.assertIn("--jq", line)
                self.assertNotIn("--method", line)
        self.assertIn("required_status_checks", normalized)

        self.assertIn("github.writes: false", section)
        self.assertIn("rollback", normalized)
        self.assertIn("advisory", normalized)
        self.assertIn("neutral", normalized)
        self.assertIn("never appears", normalized)
        self.assertIn("doctor", normalized)
        self.assertIn(REVIEW_CHECK_NAME, section)

    def test_capability_maps_agree_and_grant_no_dismissal_or_administration(self):
        documented = _documented_capabilities()
        worker = _worker_capabilities()
        self.assertTrue(worker)
        self.assertEqual(set(documented), set(worker))
        self.assertEqual(documented, worker)
        self.assertEqual(_python_capabilities(), set(worker))

        permissions = {
            entry.split(": ", 1)[0] for row in worker.values() for entry in row
        }
        self.assertEqual(permissions, {"pull_requests", "checks", "contents"})
        self.assertNotIn("administration", permissions)

        # The product holds no dismissal surface at all: no source, Worker, or
        # deployment file may reach GitHub's review-dismissal endpoint.
        sources = [
            *sorted((ROOT / "src").glob("**/*.py")),
            *sorted((ROOT / "deploy" / "cloudflare" / "src").glob("**/*.ts")),
            *sorted((ROOT / "deploy" / "cloudflare" / "test").glob("**/*.ts")),
        ]
        self.assertTrue(sources)
        for path in sources:
            with self.subTest(path=path.relative_to(ROOT).as_posix()):
                self.assertNotIn("dismissals", path.read_text(encoding="utf-8"))

    def test_doctor_gate_line_matches_the_documented_sample(self):
        report = run_doctor()
        gate = next(
            check for check in report["checks"] if check["name"] == "review-gate"
        )
        self.assertIn("requests no Administration permission", gate["detail"])
        self.assertIn(
            "never changes branch protection",
            _normalized(gate["detail"]),
        )
        self.assertIn(gate["detail"], INSTALLATION.read_text(encoding="utf-8"))
        self.assertIn("0056-host-fact-placement", GATE_ADR.read_text(encoding="utf-8"))
        for path in (INSTALLATION, APP_AUTH, GATE_ADR, TOKEN_BROKER, BROKER_CLIENT):
            with self.subTest(path=path.relative_to(ROOT).as_posix()):
                self.assertNotIn(STALE_CLAIM, path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
