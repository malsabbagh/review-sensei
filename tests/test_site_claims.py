"""Guard against reintroducing misleading public website claims (issue #93)."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

SITE_ROOT = Path(__file__).resolve().parents[1] / "docs" / "site"
INDEX = SITE_ROOT / "index.html"
SECURITY = SITE_ROOT / "security" / "index.html"
SITEMAP = SITE_ROOT / "sitemap.xml"

BANNED_PATTERNS = [
    re.compile(r"92\s*%\s*confidence", re.I),
    re.compile(r"stores\s+nothing", re.I),
    re.compile(r"<span class=\"badge\">Verified</span>"),
]

REQUIRED_QUALIFIERS = [
    "illustrative",
    "schema",
    "alpha",
]


class SiteClaimsTests(unittest.TestCase):
    def test_security_page_exists(self) -> None:
        self.assertTrue(SECURITY.is_file())

    def test_sitemap_lists_security(self) -> None:
        text = SITEMAP.read_text(encoding="utf-8")
        self.assertIn("https://reviewsensei.dev/security/", text)

    def test_index_has_no_banned_claims(self) -> None:
        text = INDEX.read_text(encoding="utf-8")
        for pattern in BANNED_PATTERNS:
            self.assertIsNone(
                pattern.search(text),
                f"banned claim matched {pattern.pattern}",
            )

    def test_index_links_security_page(self) -> None:
        text = INDEX.read_text(encoding="utf-8")
        self.assertIn("/security/", text)

    def test_index_qualifies_hero_preview(self) -> None:
        text = INDEX.read_text(encoding="utf-8").lower()
        self.assertIn("illustrative", text)

    def test_security_explains_validation_layers(self) -> None:
        text = SECURITY.read_text(encoding="utf-8")
        for term in (
            "schema validation",
            "Approval eligibility",
            "does not persist raw prompts",
        ):
            self.assertIn(term, text)

    def test_security_links_canonical_docs(self) -> None:
        text = SECURITY.read_text(encoding="utf-8")
        self.assertIn("docs/data-handling.md", text)
        self.assertIn("SECURITY.md", text)


if __name__ == "__main__":
    unittest.main()
