"""Static site checks for the getting-started onboarding page."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
GETTING_STARTED = REPO_ROOT / "docs" / "site" / "getting-started" / "index.html"

PATH_LABELS = (
    "App-assisted GitHub reviews",
    "Manual Actions setup",
    "Local CLI",
)

MATRIX_TERMS = (
    "analysis",
    "comment publication",
    "automatic approval",
    "request changes",
    "learning proposal generation",
    "learning PR publication",
    "conversations",
    "artifact retention",
)

VERSION_PATTERNS = (
    re.compile(r"review-sensei==0\.6\.6"),
    re.compile(r"review-sensei\s+0\.6\.6"),
    re.compile(r"@reviewsensei/cli@0\.6\.6"),
    re.compile(r"REVIEWSENSEI_VERSION=0\.6\.6"),
)

# Patterns that would indicate a credential value leaked into static docs.
CREDENTIAL_VALUE_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"gho_[A-Za-z0-9]{20,}"),
    re.compile(r"ghs_[A-Za-z0-9]{20,}"),
    re.compile(r"-----BEGIN (?:RSA |EC )?PRIVATE KEY-----"),
    re.compile(r"OLLAMA_API_KEY\s*=\s*['\"]?[A-Za-z0-9_\-]{8,}"),
    re.compile(r"GITHUB_TOKEN\s*=\s*['\"]?[A-Za-z0-9_\-]{8,}"),
    re.compile(r"OPENAI_API_KEY\s*=\s*['\"]?[A-Za-z0-9_\-]{8,}"),
)


class GettingStartedSiteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = GETTING_STARTED.read_text(encoding="utf-8")
        cls.lower_html = cls.html.casefold()

    def test_page_exists(self) -> None:
        self.assertTrue(GETTING_STARTED.is_file(), "getting-started page must exist")

    def test_contains_all_three_paths(self) -> None:
        for label in PATH_LABELS:
            self.assertIn(label, self.html, f"missing installation path: {label}")

    def test_contains_control_matrix_terms(self) -> None:
        for term in MATRIX_TERMS:
            self.assertIn(
                term.casefold(),
                self.lower_html,
                f"missing control matrix term: {term}",
            )

    def test_snippets_reference_released_version(self) -> None:
        matched = [
            pattern.pattern for pattern in VERSION_PATTERNS if pattern.search(self.html)
        ]
        self.assertTrue(
            matched,
            "page must reference review-sensei 0.6.6 or @reviewsensei/cli@0.6.6",
        )

    def test_no_credential_values(self) -> None:
        for pattern in CREDENTIAL_VALUE_PATTERNS:
            self.assertIsNone(
                pattern.search(self.html),
                f"possible credential value matched: {pattern.pattern}",
            )

    def test_sitemap_includes_getting_started(self) -> None:
        sitemap = (REPO_ROOT / "docs" / "site" / "sitemap.xml").read_text(
            encoding="utf-8"
        )
        self.assertIn("https://reviewsensei.dev/getting-started/", sitemap)


if __name__ == "__main__":
    unittest.main()
