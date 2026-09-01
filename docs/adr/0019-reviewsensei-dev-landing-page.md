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

The site is a single static HTML file with inline CSS and JS. No build step or
framework is required. The page's core content and behavior remain
self-contained; the approved Google Tag Manager bootstrap is the sole external
runtime dependency and can load code managed outside the reviewed repository.
This keeps the deployment surface minimal while making the analytics boundary
explicit.

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

## Amendment - Google Tag Manager analytics (PR #92)

Date: 2026-09-01

### Context

The landing page now uses the standard Google Tag Manager bootstrap for
container `GTM-5W7TJV38` to support approved site analytics. This introduces a
third-party runtime boundary: the reviewed HTML contains the bootstrap, but
the remote container can change which tags execute between repository
commits. The original self-contained-page statement therefore needs an
explicit exception and operating controls.

### Decision

Keep the page as static HTML under `docs/site/` and deploy it through the
existing GitHub Pages workflow. The approved integration is limited to the
literal GTM loader and its matching noscript iframe in
`docs/site/index.html`; the container ID and Google endpoints cannot be
selected by visitors or workflow inputs. This amendment narrows the original
"no external runtime dependency" statement: the page's core content, CSS, and
interaction code remain in the reviewed file, while GTM is an intentional
external analytics dependency.

### Ownership and privacy

ReviewSensei maintainers own the repository bootstrap, container ID,
data-layer contract, and privacy requirements. GTM workspace administrators
own the remote container's tags, versions, publish permissions, and emergency
pause/revert operations. GitHub Pages owns delivery of the reviewed bootstrap
after a merge to `main`.

A visit can send ordinary request metadata (including source IP address, user
agent, referrer, and cookies) to Google, and the published tags may collect
additional data. The page must not put source code, diffs, prompts, review
comments, credentials, or other sensitive ReviewSensei data in `dataLayer`.
Published tags must comply with the site's privacy notice and applicable
consent requirements.

### Controls

- The container ID and both Google endpoints are literal values reviewed in
  the normal PR process.
- The standard loader and noscript fallback are the only GTM integration in
  the page; new tags or data fields require maintainer review in the GTM
  workspace and repository privacy-policy review.
- GTM workspace access and publish permissions are restricted to designated
  maintainers, and approved container versions are retained for audit.
- The architecture and data-handling documentation must remain consistent
  with any future analytics or data-layer change.

### Rollout and rollback

Roll out through the normal reviewed PR and GitHub Pages deployment. For an
active analytics incident, the GTM workspace owner can pause the container or
restore the last approved container version. Remove the repository integration
by reverting the two snippets in a reviewed PR, merging to `main`, and
verifying that the deployed page no longer requests the GTM endpoints. No
review-engine data migration is required.
