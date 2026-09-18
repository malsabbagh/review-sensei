# ADR 0038 - Bind release artifacts in a compatibility manifest

Status: Proposed
Date: 2026-09-15
GitHub Issue: #35
Pull Request: not configured
Owners/Reviewers: Maintainers
Approved by: not applicable

## Context

The reusable workflow installs an exact PyPI version with an executing-commit
fallback and checks the version string. The broker separately verifies the
managed workflow ref and SHA. A version string alone does not bind the
workflow commit, Python distributions, npm artifacts, public schemas, and
Worker identity into one supported combination. A digest fetched beside an
untrusted artifact is not sufficient authentication.

## Decision

Publish a versioned compatibility manifest that records exact SHA-256 digests
and a 40-character workflow commit. The manifest may only name an explicit
trusted provenance mechanism: GitHub artifact attestation, PyPI Trusted
Publishing, or npm OIDC provenance. Build, verify, canary-bind, then
promote `v4`. Missing, extra, or mismatched artifact identities fail closed
before execution or channel promotion.

PyPI-primary installs prove the Python artifact digest and the executing
workflow commit against that manifest. The executing-commit fallback is
allowed only when pip reports that `review-sensei==X.Y.Z` has no matching
distribution; network and authentication failures remain distinct and do not
change source. The fallback still proves the executing commit and installed
version.

`v4` remains the only movable operator-managed channel, as restricted by the
protection-policy contract from #26. Immutable `vX.Y.Z` package tags cannot
be replaced. Promotion is serialized and audited. Rollback records the
previous and new channel targets and uses previously published immutable
artifacts. Partial platform publication cannot move `v4`.

In-flight `v4` movement during a run fails closed. Bounded grace exists only
as an explicit authorized record that names both SHAs; it is not the default
consumer path.

## Scope

In scope:

- Compatibility-manifest schema and validation, including exact digests and
  trusted provenance.
- Build/verify APIs and scripts that reject missing or ambiguous combinations.
- PyPI and executing-commit identity proof, with distinct install-failure
  classes.
- Canary-binding and channel-promotion/rollback records.
- Tests for mismatched digests, outdated Worker compatibility, malformed
  manifests, partial publication, and in-flight tag movement.
- Public installation, release, architecture, and contract documentation.

Out of scope:

- Expanding write permissions, cloud egress, trusted context, or automatic
  approval.
- Automatically approving or moving `v4`.
- A live disposable-repository canary. That remains operator-only and is
  coordinated with #34; this change records the binding contract only.
- Marking this ADR Accepted.

## Consequences

Positive:

- Supported release combinations are machine-checked instead of inferred from
  a version string.
- Consumer install failures stay diagnosable.
- `v4` promotions have an auditable previous/new target pair bound to one
  manifest digest.

Tradeoffs:

- A complete manifest still requires operator-assembled artifact files from
  the Python, npm, and Worker lanes.
- Live canary evidence cannot authorize promotion in this change.

## Alternatives considered

Fetching a checksum file next to an untrusted package was rejected because it
does not authenticate the publisher. Falling back to GitHub on every PyPI
error was rejected because it hides network and authentication failures.
Treating `v4` like an immutable package tag was rejected because it is the
intentional moving channel, with a separate audit record.

## Validation

- Unit tests cover build output, digest mismatches, outdated Worker ranges,
  malformed/sidecar provenance, PyPI and executing-commit identity, install
  failure classification, partial publication, serialized promotion, rollback
  targets, immutable tag replacement, and in-flight tag movement.
- Schema validation remains fail-closed through the public document loader.

## Rollout and rollback

Rollout is a normal pull request. Operators may assemble and validate a
manifest after all release lanes have immutable artifacts; they must not move
`v4` until publication is complete and canary evidence binds that digest.
Rollback of this change is code-only: revert the schemas, validators, scripts,
and documentation. Do not reuse a published package version.

## Follow-up

- Operator-owned live canary from #34, using the binding contract added here.
- Operator-owned `v4` tag protection evidence from #26.
- Optional consumer-side attestation verification during install, without
  fetching an unauthenticated sidecar digest.

## Amendment - public channel `v5`

The movable operator-managed write channel is now `v5`. Do not move it until
publication is complete and canary evidence binds the digest. Immutable
`vX.Y.Z` package tags remain unreplaceable. The broker still accepts `v4`
during migration. The protection-policy `v4_promotion` ledger record type is
a historical schema name.
