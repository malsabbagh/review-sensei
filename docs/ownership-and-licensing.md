# Ownership and licensing

Project: ReviewSensei  
Last updated: 2026-09-16  
Related: [issue #89](https://github.com/malsabbagh/review-sensei/issues/89),
[`TRADEMARKS.md`](../TRADEMARKS.md),
[ADR 0040](adr/0040-community-managed-enterprise-boundary.md)

This document inventories copyright licensing, brand identity, and operational
control for ReviewSensei. It does **not** change the root
[`LICENSE`](../LICENSE), add commercial restrictions to existing MIT-covered
source, register a trademark, create a corporate assignee, or authorize moving
published code behind a paywall.

Public rights-holder and policy wording here is intended for maintainer review
before merge. Unverified external facts are marked explicitly.

## Rights holder (copyright notice)

The root MIT license notice is:

> Copyright (c) 2026 Mohamad Alsabbagh

That notice is the maintainer-approved copyright line for the project’s own
MIT-licensed publications (`pyproject.toml` authors,
`packages/npm/*/LICENSE`, and the root `LICENSE`). It does **not** mean Mohamad
Alsabbagh exclusively owns every contributor’s copyright interest. Contributors
(or their employers/rightsholders) retain ownership of their contributions
unless a separate written agreement says otherwise. See
[`CONTRIBUTING.md`](../CONTRIBUTING.md).

Other rightsholders’ notices, where present in third-party dependencies or
vendored material, must be preserved. This repository does not invent a
corporate entity or assume copyright assignment.

## Inventory

### Engine, CLI, adapters, schemas, workflows, and public App/broker source

| Area | Paths (representative) | Current license treatment |
| --- | --- | --- |
| Review engine, CLI, providers, schemas, packaging | `src/review_sensei/`, `setup.py`, `pyproject.toml`, `packaging/` | MIT via root [`LICENSE`](../LICENSE). Verified in [`pyproject.toml`](../pyproject.toml): `license = "MIT"` and `license-files = ["LICENSE"]` (PEP 621 SPDX string form, not a `{ text = ... }` table). |
| Example workflows | `examples/github-actions/` | MIT with the Software (documentation/examples shipped in the tree) |
| GitHub App hosting helpers | `src/review_sensei/hosting/` | MIT (same root license) |
| Cloudflare Worker / Durable Object App + OIDC broker package | `deploy/cloudflare/` | MIT with the repository Software; package is `private` npm metadata and has no separate LICENSE file—root MIT applies. Dependency licenses remain those of the locked packages in `package-lock.json` and are **not** redeclared as MIT. |
| npm launcher and platform packages | `packages/npm/**` | MIT; each published package ships its own `LICENSE` copy matching the root notice |

**Preserve MIT coverage.** Publishing these trees does **not** grant exclusive
control of any official hosted deployment. Operators may self-host the App and
broker code under MIT; maintainers’ operation of an official instance is
account/ops control, not a proprietary reclassification of the published
source. See [ADR 0040](adr/0040-community-managed-enterprise-boundary.md).

### Evaluation data and third-party material

| Material | Location / evidence | Treatment |
| --- | --- | --- |
| Synthetic evaluation corpus v1 | `evaluation/v1/`, `corpus.json` `"license": "CC0-1.0"`, [`evaluation/v1/README.md`](../evaluation/v1/README.md), [`docs/data-handling.md`](data-handling.md) | **CC0-1.0**, not MIT. Do not label the corpus MIT. |
| Third-party npm/Python dependencies | lockfiles and installed package metadata | Retain each dependency’s own license; do not flatten to MIT. |
| Pinned GitHub Actions | `.github/workflows/`, `examples/github-actions/` | Upstream Action licenses apply to those Actions; pins are supply-chain references, not a license change for ReviewSensei source. |

### Name, logos, icons, and website assets

| Asset | Provenance (repository evidence) | Copyright vs trademark |
| --- | --- | --- |
| Product name “ReviewSensei” | [ADR 0016](adr/0016-product-identity-rename-to-reviewsensei.md) | Trademark/source-identification rules: [`TRADEMARKS.md`](../TRADEMARKS.md). Copyright in documentation text follows the applicable file license (typically MIT). |
| Site favicon / logo SVG | [`docs/site/assets/favicon.svg`](site/assets/favicon.svg); [ADR 0019](adr/0019-reviewsensei-dev-landing-page.md) describes an SVG derived from the landing-page prototype | **Copyright:** treated as project Software under root MIT unless a file-level notice says otherwise. **Uncertainty:** no separate artist assignment or trademark registration record appears in the repository; possession of the file alone does not prove exclusive copyright eligibility or cleared trademark rights. |
| Landing page HTML/CSS/JS | [`docs/site/`](site/) | MIT with the Software; GTM bootstrap is an external runtime dependency noted in ADR 0019. |

Existing copyright permissions for logo/image files are **not** withdrawn by the
trademark policy. Any future change to asset licenses requires a provenance
review and an explicitly scoped, maintainer-approved decision.

### Official domain, GitHub App, packages, releases, and hosted endpoints

| Channel | Canonical identifier (documented) | Operational notes / verification status |
| --- | --- | --- |
| Public repository | `https://github.com/malsabbagh/review-sensei` | Owner login `malsabbagh` is visible in repository metadata. |
| Website / domain | `https://reviewsensei.dev` (`docs/site/CNAME`) | Canonical marketing site via GitHub Pages ([ADR 0019](adr/0019-reviewsensei-dev-landing-page.md)). **DNS registrar and account recovery control: not verified from repository evidence alone.** |
| GitHub App | `https://github.com/apps/reviewsensei` (linked from the site and registration docs) | Registration/branding docs in [`docs/github-app-registration.md`](github-app-registration.md). **App owner account and recovery control: not verified beyond public App URL and docs.** |
| PyPI | project name `review-sensei` | Publication runbook: [`docs/releasing.md`](releasing.md). **Publisher account recovery: not assessed in-repo.** |
| npm scope / packages | `@reviewsensei/cli` and platform packages | Same releasing docs; Trusted Publishing / bootstrap described there. **npm org recovery: not assessed in-repo.** |
| GitHub Releases / tags | repository Releases and protected tags (for example setup `v4`) | Maintainer release authority per [`CONTRIBUTING.md`](../CONTRIBUTING.md) and [`docs/releasing.md`](releasing.md). |
| Cloudflare Worker / broker endpoints | Operator-deployed Workers (see `deploy/cloudflare/`) | Architecture notes that no live account is configured **in this repository**. Official production hostnames, if any, are operational—not proven exclusive software copyright. |

Distinguish **operational/account control** (who can publish packages, move
tags, or run a Worker) from **software copyright** (who may use the MIT-licensed
source under its terms). Control of an official channel does not make the
published implementation proprietary.

### Future commercial implementation

Prospective, separately developed commercial features (organization dashboard,
central policy, cross-repository reporting, SSO/SCIM/RBAC administration, audit
management, billing, managed operations, and support) are described in
[ADR 0040](adr/0040-community-managed-enterprise-boundary.md). They are **not**
implemented here and must not reclassify already published MIT (or CC0) material
as proprietary.

## Maintainer decision checklist

Documented for maintainer action **without** performing external account
changes in this issue:

1. **Rights holder and branding provenance**  
   Confirm Mohamad Alsabbagh remains the correct public copyright notice and
   trademark claimant wording. Review logo/favicon provenance; do not infer
   copyright eligibility solely from possession of an image file.

2. **Name availability / clearance**  
   Review name availability and conflicts; decide whether professional clearance
   and Canadian registration (CIPO) are appropriate; consider other jurisdictions
   only where commercially relevant. **Status: not assessed** until checked. An
   ordinary web search is not formal clearance.

3. **Administrative / recovery control**  
   Confirm administrative and recovery control for:
   - domain `reviewsensei.dev`
   - GitHub App `reviewsensei`
   - PyPI `review-sensei`
   - npm `@reviewsensei` scope
   - release publication (GitHub Environments / Trusted Publishing)  
   Use existing runbooks ([`docs/releasing.md`](releasing.md),
   [`docs/github-app-registration.md`](github-app-registration.md),
   [`docs/github-app-auth.md`](github-app-auth.md)) rather than duplicating them.
   Never record credentials or recovery secrets in the repository.

4. **Deferred decisions**  
   Record deferred registration or contribution-policy decisions explicitly.
   Optional external trademark filing is **not** required to complete this
   documentation work. Contribution policy decision for this issue:
   **DCO retained; no CLA introduced** (see [`CONTRIBUTING.md`](../CONTRIBUTING.md)).

## Cross-links

- Trademark usage: [`TRADEMARKS.md`](../TRADEMARKS.md)
- Contribution licensing and DCO: [`CONTRIBUTING.md`](../CONTRIBUTING.md)
- Community / Managed / Enterprise boundary: [ADR 0040](adr/0040-community-managed-enterprise-boundary.md)
- Evaluation privacy and CC0 corpus: [`docs/data-handling.md`](data-handling.md), [`docs/evaluation.md`](evaluation.md)
- Release channels: [`docs/releasing.md`](releasing.md)
