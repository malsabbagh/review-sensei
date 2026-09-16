# ADR 0040 - Community / Managed / Enterprise product boundary

Status: Proposed
Date: 2026-09-16
GitHub Issue: #89
Pull Request: not configured
Owners/Reviewers: Maintainers
Approved by: not configured

## Context

ReviewSensei’s published tree is MIT-licensed software (with a CC0-1.0 synthetic
evaluation corpus). Maintainers also operate or document official brand,
package, and optional App/broker channels. Contributors and operators need a
clear boundary so that:

- existing open-source capabilities stay available under their current licenses;
- official identity and release channels remain under maintainer operational
  control without reclassifying published source as proprietary; and
- any future commercial layer is designed as **new, separately licensed
  implementation**, not a retroactive paywall on Community code.

Issue #89 asks for this boundary as a maintainer-approval proposal. It does not
authorize implementing billing, closing source, or changing runtime behavior.

## Decision

Adopt the following product boundary for maintainer approval:

### Community

Available under existing licenses (primarily MIT; evaluation corpus CC0-1.0):

- the review engine, CLI, provider adapters, schemas, and workflows;
- public GitHub App / Cloudflare Worker / OIDC broker **source** as published in
  this repository;
- documented self-hosting and operator-owned deployment of that source.

No existing Community capability is moved, deleted, closed-source, or paywalled
by this ADR.

### Official operations and identity

Maintainers control official service access (if any), package publication, release
channels, and branding as described in
[`docs/ownership-and-licensing.md`](../ownership-and-licensing.md) and
[`TRADEMARKS.md`](../../TRADEMARKS.md). That operational control does **not**
make the published implementation exclusive or proprietary.

### Potential future commercial layer (Managed / Enterprise)

The following are **possible product boundaries**, not implemented features or
delivery promises:

- organization dashboard;
- central configuration and policy management;
- cross-repository reporting;
- SSO / SCIM / RBAC administration;
- audit management;
- billing;
- managed operations and support.

Newly developed proprietary implementation for that layer should live in a
**separate private repository**. Existing MIT components may be reused there
only with required copyright notices preserved. Interoperability protocols and
client contracts that Community installs rely on should remain documented.
Avoid introducing a proprietary service dependency into the existing
self-hosted Community path.

## Scope

In scope:

- Documentation of the Community / official-ops / future-commercial boundary.
- Cross-links from architecture indexes and ownership docs.
- Maintainer checklist items deferred from issue #89 (registration, account
  recovery confirmation) without performing those external actions here.

Out of scope:

- Changing the root MIT license or adding noncommercial / anti-SaaS terms.
- Implementing Managed/Enterprise features, billing, or a hosted review service.
- Creating the private commercial repository.
- Moving, deleting, or paywalling existing public functionality.
- Changing package names, release channels, CLI/API contracts, workflow tags,
  endpoint access, or runtime behavior.
- Introducing a CLA or copyright assignment.

## Alternatives considered

### Relabel public App/broker source as proprietary

Rejected. The Cloudflare/App/broker trees are already published under the
repository’s MIT terms. Relabeling would break license consistency and
self-hosting expectations.

### Require a CLA before any commercial product

Rejected for this issue. MIT already permits commercial reuse and sublicensing
subject to notice conditions. A DCO certifies the right to contribute; it is not
assignment. A future CLA would need a documented need, reviewed terms,
maintainer approval, and prospective adoption only.

### Keep boundaries undocumented

Rejected. Ambiguity between brand/ops control and copyright licensing causes
contributor and operator confusion.

## Consequences

Positive:

- Community users retain clear rights to self-host and reuse existing MIT code.
- Maintainers can plan a commercial layer without rewriting history.
- Trademark and copyright documentation stay aligned.

Tradeoffs:

- Commercial features remain unimplemented until separately staffed and
  licensed.
- Account ownership and trademark registration facts stay explicitly unverified
  until maintainers complete external checks.

## Validation

- Documentation and ADR cross-links land with issue #89.
- Root `LICENSE` text unchanged; Python/npm license metadata unchanged.
- No runtime, package identity, or workflow-tag changes in the implementing PR.
- `git diff --check` and applicable documentation/site review pass.

## Rollout and rollback

Rollout is documentation-only: merge the ownership, trademark, contribution,
and ADR updates after maintainer review of public policy wording. Rollback is
revert of those documentation commits; no data migration or release is
required.

## Follow-up work

- Maintainer approval of public rights-holder and trademark wording.
- Optional professional clearance / Canadian (or other) trademark registration
  decisions, recorded as “assessed” when done.
- Confirm administrative recovery for domain, GitHub App, PyPI, npm, and release
  publishing using existing runbooks—without committing secrets.
- If a commercial repository is created later, document its license boundary and
  keep Community self-hosting free of proprietary service hard dependencies.
