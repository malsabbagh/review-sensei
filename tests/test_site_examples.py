from __future__ import annotations

import json
import re
import unittest
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

from review_sensei.schemas import validate_public_document

ROOT = Path(__file__).resolve().parents[1]
SITE_ROOT = ROOT / "docs" / "site"
EXAMPLES_PAGE = SITE_ROOT / "examples" / "index.html"
HOMEPAGE = SITE_ROOT / "index.html"
SECURITY_PAGE = SITE_ROOT / "security" / "index.html"
GETTING_STARTED_PAGE = SITE_ROOT / "getting-started" / "index.html"
SITE_PAGES = (HOMEPAGE, EXAMPLES_PAGE, SECURITY_PAGE, GETTING_STARTED_PAGE)

# Pages that pull the shared stylesheet versus pages that carry their own
# inline styles. Every page in SITE_PAGES must appear in exactly one group.
SHARED_LAYOUT_PAGES = (EXAMPLES_PAGE, SECURITY_PAGE)
INLINE_STYLE_PAGES = (HOMEPAGE, GETTING_STARTED_PAGE)

# Internal routes each page is allowed to link before the target ships, scoped
# per page so an allowance on one page cannot hide a 404 on another.
# ``test_pending_site_routes_are_still_missing`` fails once a listed route
# resolves, so shipping the page forces its removal from this map rather than
# letting the allowance outlive the gap it covers. Prefer an empty set: only
# add a route when the link must ship ahead of its target.
PENDING_SITE_ROUTES: dict[Path, frozenset[str]] = {
    HOMEPAGE: frozenset(),
    EXAMPLES_PAGE: frozenset(),
    SECURITY_PAGE: frozenset(),
    GETTING_STARTED_PAGE: frozenset(),
}


def _pending_routes(page: Path) -> frozenset[str]:
    return PENDING_SITE_ROUTES.get(page, frozenset())


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
        self.stylesheets: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name: value for name, value in attrs if value}
        if tag == "a":
            href = attributes.get("href")
            if href:
                self.hrefs.append(href)
        elif tag == "link":
            href = attributes.get("href")
            if not href:
                return
            self.assets.append(href)
            rel = (attributes.get("rel") or "").lower().split()
            if "stylesheet" in rel:
                self.stylesheets.append(href)
        elif tag in {"script", "img", "source"}:
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
        """Every internal link on every tracked page must resolve."""

        for page in SITE_PAGES:
            pending = _pending_routes(page)
            internal = [href for href in _collect(page).hrefs if not _is_external(href)]
            label = str(page.relative_to(SITE_ROOT))
            with self.subTest(page=label, stage="collected"):
                self.assertTrue(internal, f"{page} exposes no internal links")
            for href in internal:
                if href.split("#", 1)[0] in pending:
                    continue
                resolved = _resolve_site_path(href, page=page)
                if resolved is None:
                    continue
                with self.subTest(page=label, href=href):
                    self.assertTrue(
                        resolved.is_file(),
                        f"missing target for {href}: {resolved}",
                    )

    def test_pending_site_routes_are_still_missing(self) -> None:
        """Force the per-page allowance to shrink as pages ship.

        A route that now resolves must be removed from its
        ``PENDING_SITE_ROUTES`` entry so the link checks actually cover it.
        """

        for page, routes in PENDING_SITE_ROUTES.items():
            label = str(page.relative_to(SITE_ROOT))
            for route in sorted(routes):
                resolved = _resolve_site_path(route, page=page)
                with self.subTest(page=label, route=route):
                    self.assertIsNotNone(resolved, route)
                    assert resolved is not None
                    self.assertFalse(
                        resolved.is_file(),
                        f"{route} now exists; remove it from "
                        f"PENDING_SITE_ROUTES[{label}]",
                    )

    def test_pending_routes_are_scoped_to_tracked_pages(self) -> None:
        """The allowance map may not carry entries for untracked pages."""

        for page in PENDING_SITE_ROUTES:
            with self.subTest(page=str(page)):
                self.assertIn(page, SITE_PAGES)
                self.assertTrue(page.is_file(), page)

    def test_nav_does_not_link_missing_routes(self) -> None:
        """Pages added by this PR must not ship a 404 in their nav."""

        for page in (HOMEPAGE, EXAMPLES_PAGE, SECURITY_PAGE):
            label = str(page.relative_to(SITE_ROOT))
            with self.subTest(page=label):
                self.assertFalse(
                    _pending_routes(page),
                    f"{label} must not rely on pending routes in its nav",
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

    def test_site_css_declares_no_unused_tokens(self) -> None:
        """Custom properties must ship with the rule that consumes them.

        Only ``site.css`` and the pages that link it resolve these tokens, so
        usage is counted across exactly that set.
        """

        stylesheet = SITE_ROOT / "assets" / "site.css"
        css = stylesheet.read_text(encoding="utf-8")
        root = re.search(r":root\{(.*?)\}", css, re.DOTALL)
        self.assertIsNotNone(root, "site.css must declare a :root block")
        assert root is not None
        declared = re.findall(r"(--[a-z0-9-]+)\s*:", root.group(1))
        self.assertTrue(declared, "site.css declares no tokens")
        consumers = css + "\n".join(
            page.read_text(encoding="utf-8") for page in SHARED_LAYOUT_PAGES
        )
        for token in declared:
            with self.subTest(token=token):
                self.assertRegex(
                    consumers,
                    r"var\(\s*" + re.escape(token) + r"\s*[,)]",
                    f"{token} is declared but never used; drop it or land it "
                    "with its first consumer",
                )

    def test_stylesheet_references_resolve(self) -> None:
        """Every ``<link rel="stylesheet">`` target must exist."""

        for page in SITE_PAGES:
            label = str(page.relative_to(SITE_ROOT))
            for reference in _collect(page).stylesheets:
                if _is_external(reference):
                    continue
                resolved = _resolve_site_path(reference, page=page)
                with self.subTest(page=label, stylesheet=reference):
                    self.assertIsNotNone(resolved, reference)
                    assert resolved is not None
                    self.assertTrue(
                        resolved.is_file(),
                        f"missing stylesheet for {reference}: {resolved}",
                    )

    def test_shared_layout_pages_reference_site_css(self) -> None:
        """A shared-layout page cannot silently fall back to inline styles.

        Pages are classified explicitly, so moving a page between the shared
        stylesheet and inline styles has to update this classification rather
        than quietly passing.
        """

        stylesheet = (SITE_ROOT / "assets" / "site.css").resolve()
        self.assertTrue(stylesheet.is_file(), stylesheet)
        self.assertEqual(
            set(SHARED_LAYOUT_PAGES) | set(INLINE_STYLE_PAGES),
            set(SITE_PAGES),
            "every tracked page must be classified exactly once",
        )
        self.assertFalse(set(SHARED_LAYOUT_PAGES) & set(INLINE_STYLE_PAGES))
        for page in SITE_PAGES:
            resolved = {
                candidate.resolve()
                for candidate in (
                    _resolve_site_path(reference, page=page)
                    for reference in _collect(page).stylesheets
                    if not _is_external(reference)
                )
                if candidate is not None
            }
            label = str(page.relative_to(SITE_ROOT))
            with self.subTest(page=label):
                if page in SHARED_LAYOUT_PAGES:
                    self.assertIn(
                        stylesheet,
                        resolved,
                        f"{label} uses the shared layout and must link site.css",
                    )
                else:
                    self.assertNotIn(
                        stylesheet,
                        resolved,
                        f"{label} links site.css; move it to SHARED_LAYOUT_PAGES",
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
        assert match is not None
        embedded = json.loads(match.group(1).strip())
        expected_path = ROOT / "evaluation/v1/expected/off-by-one.review.json"
        expected = json.loads(expected_path.read_text(encoding="utf-8"))
        self.assertEqual(embedded, expected)

    def test_off_by_one_rendered_block_matches_embedded_payload(self) -> None:
        """The visible <pre> copy must not drift from the validated payload.

        The off-by-one JSON is duplicated: once in the schema-validated
        ``<script>`` block and once in the ``<pre><code>`` block readers see.
        Only the script copy is schema-checked, so assert the rendered copy
        matches it exactly.
        """

        html = EXAMPLES_PAGE.read_text(encoding="utf-8")
        script = re.search(
            r'id="example-off-by-one">(.*?)</script>',
            html,
            re.DOTALL,
        )
        rendered = re.search(
            r'<pre[^>]*aria-label="Expected off-by-one review JSON"[^>]*>'
            r"<code>(.*?)</code></pre>",
            html,
            re.DOTALL,
        )
        self.assertIsNotNone(script)
        self.assertIsNotNone(rendered, "rendered off-by-one block not found")
        assert script is not None
        assert rendered is not None
        self.assertEqual(
            unescape(rendered.group(1)).strip(),
            script.group(1).strip(),
            "visible off-by-one block drifted from the validated payload",
        )


if __name__ == "__main__":
    unittest.main()
