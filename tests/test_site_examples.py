from __future__ import annotations

import json
import re
import unittest
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

from review_sensei.schemas import validate_public_document

ROOT = Path(__file__).resolve().parents[1]
SITE_ROOT = ROOT / "docs" / "site"
EXAMPLES_PAGE = SITE_ROOT / "examples" / "index.html"
HOMEPAGE = SITE_ROOT / "index.html"
SECURITY_PAGE = SITE_ROOT / "security" / "index.html"
SITE_PAGES = (HOMEPAGE, EXAMPLES_PAGE, SECURITY_PAGE)

# Internal routes that are allowed to 404 because the page has not shipped yet.
# ``test_pending_site_routes_are_still_missing`` fails once a route here exists,
# so shipping the page forces its removal instead of letting the allowance
# silently outlive the gap it was meant to cover. Keep this set empty when
# every linked route resolves.
PENDING_SITE_ROUTES = {"/getting-started/"}

JSON_SCRIPT_RE = re.compile(
    r'<script[^>]+type="application/json"[^>]+data-schema="([^"]+)"[^>]*>(.*?)</script>',
    re.DOTALL,
)


class _LinkCollector(HTMLParser):
    """Collect anchor targets and static asset references separately.

    Anchor hrefs drive navigation checks. ``<link href>`` and ``<script src>``
    are relative asset references that break when a page moves between ``/``,
    ``/examples/``, and ``/security/``, so they are collected too.
    """

    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []
        self.assets: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name: value for name, value in attrs if value}
        if tag == "a":
            href = attributes.get("href")
            if href:
                self.hrefs.append(href)
        elif tag == "link":
            href = attributes.get("href")
            if href:
                self.assets.append(href)
        elif tag == "script":
            source = attributes.get("src")
            if source:
                self.assets.append(source)


def _collect(page: Path) -> _LinkCollector:
    parser = _LinkCollector()
    parser.feed(page.read_text(encoding="utf-8"))
    return parser


def _is_external(reference: str) -> bool:
    if not reference.startswith("http://") and not reference.startswith("https://"):
        return False
    netloc = urlparse(reference).netloc
    return netloc not in {"reviewsensei.dev", "www.reviewsensei.dev"}


def _resolve_site_path(href: str, *, page: Path) -> Path | None:
    if (
        href.startswith("#")
        or href.startswith("mailto:")
        or href.startswith("javascript:")
    ):
        return None
    if href.startswith("http://") or href.startswith("https://"):
        parsed = urlparse(href)
        if parsed.netloc and parsed.netloc not in {
            "reviewsensei.dev",
            "www.reviewsensei.dev",
        }:
            return None
        href = parsed.path
    href = href.split("#", 1)[0]
    if not href:
        return None
    if href.startswith("/"):
        target = SITE_ROOT / href.lstrip("/")
    else:
        target = (page.parent / href).resolve()
        try:
            target.relative_to(SITE_ROOT.resolve())
        except ValueError:
            return None
    if target.is_dir():
        target = target / "index.html"
    return target


class SiteExamplesTests(unittest.TestCase):
    def test_examples_page_exists(self) -> None:
        self.assertTrue(EXAMPLES_PAGE.is_file(), EXAMPLES_PAGE)

    def test_homepage_links_to_examples(self) -> None:
        html = HOMEPAGE.read_text(encoding="utf-8")
        self.assertIn("/examples/", html)

    def test_fixture_and_illustration_labels_present(self) -> None:
        html = EXAMPLES_PAGE.read_text(encoding="utf-8").lower()
        self.assertIn("fixture", html)
        self.assertIn("illustration", html)
        self.assertIn("evaluation/v1", html)

    def test_internal_links_resolve(self) -> None:
        parser = _collect(EXAMPLES_PAGE)
        for href in parser.hrefs:
            normalized = href.split("#", 1)[0]
            if normalized in PENDING_SITE_ROUTES:
                continue
            resolved = _resolve_site_path(href, page=EXAMPLES_PAGE)
            if resolved is None:
                continue
            with self.subTest(href=href):
                self.assertTrue(
                    resolved.is_file(),
                    f"missing target for {href}: {resolved}",
                )

    def test_homepage_internal_links_resolve(self) -> None:
        parser = _collect(HOMEPAGE)
        internal = [href for href in parser.hrefs if not _is_external(href)]
        self.assertTrue(internal, "homepage exposes no internal links")
        for href in internal:
            if href.split("#", 1)[0] in PENDING_SITE_ROUTES:
                continue
            resolved = _resolve_site_path(href, page=HOMEPAGE)
            if resolved is None:
                continue
            with self.subTest(href=href):
                self.assertTrue(
                    resolved.is_file(),
                    f"missing target for {href}: {resolved}",
                )

    def test_pending_site_routes_are_still_missing(self) -> None:
        """Force the allowance list to shrink as pages ship.

        A route that now resolves must be removed from ``PENDING_SITE_ROUTES``
        so the link tests actually cover it.
        """

        for route in sorted(PENDING_SITE_ROUTES):
            resolved = _resolve_site_path(route, page=HOMEPAGE)
            with self.subTest(route=route):
                self.assertIsNotNone(resolved, route)
                assert resolved is not None
                self.assertFalse(
                    resolved.is_file(),
                    f"{route} now exists; remove it from PENDING_SITE_ROUTES",
                )

    def test_site_asset_references_resolve(self) -> None:
        for page in SITE_PAGES:
            parser = _collect(page)
            with self.subTest(page=page.name, stage="collected"):
                self.assertTrue(parser.assets, f"{page} references no assets")
            for reference in parser.assets:
                if _is_external(reference):
                    continue
                resolved = _resolve_site_path(reference, page=page)
                with self.subTest(
                    page=str(page.relative_to(SITE_ROOT)), asset=reference
                ):
                    self.assertIsNotNone(
                        resolved,
                        f"{reference} escapes {SITE_ROOT}",
                    )
                    assert resolved is not None
                    self.assertTrue(
                        resolved.is_file(),
                        f"missing asset for {reference}: {resolved}",
                    )

    def test_new_stylesheet_is_referenced_by_new_pages(self) -> None:
        stylesheet = SITE_ROOT / "assets" / "site.css"
        self.assertTrue(stylesheet.is_file(), stylesheet)
        for page in (EXAMPLES_PAGE, SECURITY_PAGE):
            resolved = {
                _resolve_site_path(reference, page=page)
                for reference in _collect(page).assets
                if not _is_external(reference)
            }
            with self.subTest(page=str(page.relative_to(SITE_ROOT))):
                self.assertIn(
                    stylesheet.resolve(), {r.resolve() for r in resolved if r}
                )

    def test_homepage_examples_and_security_links_resolve(self) -> None:
        parser = _collect(HOMEPAGE)
        required = {"/examples/", "/security/"}
        found = {
            href.split("#", 1)[0]
            for href in parser.hrefs
            if href.startswith("/examples/") or href.startswith("/security/")
        }
        self.assertTrue(required.issubset(found))
        for href in sorted(required):
            resolved = _resolve_site_path(href, page=HOMEPAGE)
            self.assertIsNotNone(resolved)
            self.assertTrue(resolved.is_file(), resolved)

    def test_embedded_json_validates_against_schemas(self) -> None:
        html = EXAMPLES_PAGE.read_text(encoding="utf-8")
        matches = JSON_SCRIPT_RE.findall(html)
        self.assertGreaterEqual(len(matches), 4)
        for schema_name, payload in matches:
            with self.subTest(schema=schema_name):
                document = json.loads(payload.strip())
                validate_public_document(document, schema_name)

    def test_off_by_one_fixture_matches_evaluation_expected(self) -> None:
        html = EXAMPLES_PAGE.read_text(encoding="utf-8")
        match = re.search(
            r'id="example-off-by-one">(.*?)</script>',
            html,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        embedded = json.loads(match.group(1).strip())
        expected_path = ROOT / "evaluation/v1/expected/off-by-one.review.json"
        expected = json.loads(expected_path.read_text(encoding="utf-8"))
        self.assertEqual(embedded, expected)


if __name__ == "__main__":
    unittest.main()
