"""Guard against reintroducing misleading public website claims (issue #93)."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

SITE_ROOT = Path(__file__).resolve().parents[1] / "docs" / "site"
INDEX = SITE_ROOT / "index.html"
SECURITY = SITE_ROOT / "security" / "index.html"
EXAMPLES = SITE_ROOT / "examples" / "index.html"
SITEMAP = SITE_ROOT / "sitemap.xml"

# Every page is read in full, so bound the size a single page may contribute.
MAX_PAGE_BYTES = 1024 * 1024

BANNED_PATTERNS = [
    re.compile(r"92\s*%\s*confidence", re.I),
    re.compile(r"stores\s+nothing", re.I),
    re.compile(r"<span class=\"badge\">Verified</span>"),
    re.compile(r"guarantees?\s+(?:that\s+)?(?:no|zero)\s+data", re.I),
    re.compile(r"\b(?:fully|100%)\s+(?:secure|private|accurate)\b", re.I),
]

# Honesty qualifiers each claim-bearing page must keep somewhere in its copy.
# The original "illustrative"/"schema"/"alpha" set is preserved for every page
# that makes capability claims; the security page carries one more because it
# is the page that states what the project does not promise. Getting-started is
# absent because it is instructional and makes no capability claim.
REQUIRED_QUALIFIERS = {
    INDEX: ("illustrative", "schema", "alpha"),
    SECURITY: ("illustrative", "schema", "alpha", "does not guarantee"),
    EXAMPLES: ("illustrative", "schema", "alpha"),
}

INTERNAL_LINK = re.compile(r'href="(/[^"#?]*)"')


def _is_deployed(page: Path) -> bool:
    """Skip partials and drafts, which are not routes GitHub Pages serves."""

    relative = page.relative_to(SITE_ROOT)
    return not any(part.startswith(("_", ".")) for part in relative.parts)


def _pages() -> list[Path]:
    return sorted(page for page in SITE_ROOT.rglob("*.html") if _is_deployed(page))


def _read(page: Path) -> str:
    size = page.stat().st_size
    if size > MAX_PAGE_BYTES:
        raise AssertionError(f"{page} exceeds {MAX_PAGE_BYTES} bytes")
    return page.read_text(encoding="utf-8")


def _published_routes() -> set[str]:
    """Return the site-absolute routes GitHub Pages serves from ``index.html``."""

    routes = set()
    for page in _pages():
        if page.name != "index.html":
            continue
        relative = page.parent.relative_to(SITE_ROOT).as_posix()
        routes.add("/" if relative == "." else f"/{relative}/")
    return routes


def _route_target(route: str) -> Path:
    """Map a site-absolute route to the file GitHub Pages would serve."""

    relative = route.lstrip("/")
    if not relative or route.endswith("/"):
        return SITE_ROOT / relative / "index.html"
    return SITE_ROOT / relative


class SiteClaimsTests(unittest.TestCase):
    def test_site_has_pages(self) -> None:
        self.assertIn(INDEX, _pages())
        self.assertIn(SECURITY, _pages())

    def test_sitemap_lists_security(self) -> None:
        text = SITEMAP.read_text(encoding="utf-8")
        self.assertIn("https://reviewsensei.dev/security/", text)

    def test_no_page_has_banned_claims(self) -> None:
        for page in _pages():
            text = _read(page)
            for pattern in BANNED_PATTERNS:
                with self.subTest(page=page.name, pattern=pattern.pattern):
                    self.assertIsNone(
                        pattern.search(text),
                        f"banned claim matched {pattern.pattern} in {page}",
                    )

    def test_internal_links_resolve_to_published_pages(self) -> None:
        for page in _pages():
            for route in INTERNAL_LINK.findall(_read(page)):
                target = _route_target(route)
                with self.subTest(page=page.name, route=route):
                    self.assertTrue(
                        target.exists(),
                        f"{page} links to {route}, which is not published",
                    )

    def test_sitemap_and_published_pages_agree(self) -> None:
        sitemap = SITEMAP.read_text(encoding="utf-8")
        listed = set(
            re.findall(r"<loc>https://reviewsensei\.dev(/[^<]*)</loc>", sitemap)
        )
        self.assertEqual(listed, _published_routes())

    def test_index_links_security_page(self) -> None:
        self.assertIn("/security/", _read(INDEX))

    def test_pages_keep_honesty_qualifiers(self) -> None:
        for page, qualifiers in REQUIRED_QUALIFIERS.items():
            text = _read(page).lower()
            for qualifier in qualifiers:
                with self.subTest(page=page.name, qualifier=qualifier):
                    self.assertIn(qualifier, text)

    def test_security_explains_validation_layers(self) -> None:
        text = _read(SECURITY)
        for term in (
            "schema validation",
            "Approval eligibility",
            "does not persist raw prompts",
        ):
            self.assertIn(term, text)

    def test_security_links_canonical_docs(self) -> None:
        text = _read(SECURITY)
        self.assertIn("docs/data-handling.md", text)
        self.assertIn("SECURITY.md", text)


if __name__ == "__main__":
    unittest.main()
