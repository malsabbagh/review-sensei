# ADR 0019 - ReviewSensei.dev canonical landing page

Status: Proposed
Date: 2026-08-16
GitHub Issue: #47
Pull Request: not configured
Owners/Reviewers: Maintainers
Approved by: not configured

## Context

Issue #47 calls for launching `https://reviewsensei.dev` as the canonical
public entry point for ReviewSensei. The landing page must connect visitors to
installation, first review, GitHub App setup, security model, architecture,
provider configuration, and contribution docs. All public links must target
`malsabbagh/review-sensei`, never the private upstream repository.

A prototype HTML page was provided in the issue. It needed adaptation to serve
as a production landing page with correct metadata, accessibility, and
linking.

## Decision

Ship the landing page as static HTML under `docs/site/` in the repository.
Deploy through GitHub Pages using a dedicated `pages.yml` workflow that
uploads the `docs/site` directory on pushes to `main`. A `CNAME` file in the
site root maps the custom domain `reviewsensei.dev`.

The page is adapted from the provided prototype with these changes:

- All GitHub links point to `malsabbagh/review-sensei` (repository, docs,
  security, contributing, issues).
- Canonical URL, Open Graph, Twitter Card, and JSON-LD structured data
  metadata are added for consistent brand identity and SEO.
- `robots.txt` and `sitemap.xml` are included for crawlability.
- An SVG favicon derived from the prototype logo is served from
  `docs/site/assets/favicon.svg`.
- Accessibility improvements: skip-link, semantic landmarks (`main`,
  `header`, `footer`), ARIA roles on the tab list, focus-visible outlines,
  and `aria-label` on icon-only buttons.
- The install command examples use the ReviewSensei package and CLI identity
  established by ADR 0016.

The site is a single self-contained HTML file with inline CSS and JS. No build
step, framework, or external runtime dependency is required. This keeps the
deployment surface minimal and the page fast.

## Consequences

- `docs/site/**` is included in the public publication tree through the
  existing `docs/**` classification in the audit policy.
- The `pages.yml` workflow uses immutable commit-pinned GitHub Actions and is
  scanned by `check_action_pins.py`.
- GitHub Pages must be configured for the `review-sensei` repository with the
  custom domain `reviewsensei.dev`, HTTPS enforced, and the `github-pages`
  environment.
- The site contains no references to the private upstream repository, internal
  endpoints, or private documentation.
- Site content changes are reviewed through the normal PR process before
  merging to `main`, which triggers deployment.
- The page is a marketing/entry-point surface, not a hosted review engine or
  dashboard. All actionable paths link to the public repository.

## Alternatives considered

- A static site generator (Hugo, Astro, Vite): adds build complexity and
  dependency surface for a single landing page. The prototype is already
  self-contained HTML.
- A separate repository for the site: fragments the publication boundary and
  requires a second deploy pipeline. Keeping the site in `docs/site` lets it
  flow through the existing publication audit.
- A hosted CMS or dynamic backend: contradicts the open-source-first,
  customer-owned execution model.
