# ADR 0009: CI quality and supply-chain gates

- Status: Accepted
- Date: 2026-08-11
- Issue: #3
- Pending delivery: draft pull requests
- Decision owner/reviewers: Maintainers; Sol orchestration with required Lavish plan/outcome review
- Approved by: user via Lavish plan approval (outcome approval remains a delivery gate)

## Context

The project must prove supported Python behavior, package contents, public JSON
contracts, and workflow supply-chain integrity before merge. The old workflow
covered only two platforms with mutable Action tags and did not expose one
stable required status. The runtime package must remain provider-neutral,
credential-free in tests, and dependency-free at runtime.

## Decision

Adopt one pinned CI workflow with explicit Python 3.11–3.14 compatibility
entries across Ubuntu, Windows, and macOS; dedicated quality, schema, package,
and CodeQL jobs; and a stable `Required checks` aggregate. Third-party Actions
use full commit SHAs with same-line release comments. The public ReviewSensei
reusable workflow is the deliberate exception: setup-v4 follows its protected
`@v4` tag, while the broker verifies the tag's runtime commit. Dependabot updates
GitHub Actions and pip tooling weekly from the repository root. Workflow
permissions default to `contents: read`; only CodeQL receives
`security-events: write`. Because this private repository does not have GitHub
Code Scanning enabled, CodeQL retains its generated SARIF as a workflow
artifact instead of attempting an unavailable upload.

The repository variable `ENABLE_UBICLOUD_HOSTED` controls runner selection. When
it is `true`, all Linux jobs use the `ubicloud-standard-2` runner and the
compatibility matrix includes only its Ubuntu entries; Windows and macOS lanes
are skipped before runner allocation because Ubicloud does not provide those
hosts. When the variable is unset or false, Linux jobs use GitHub-hosted
`ubuntu-latest` and the complete cross-platform matrix remains available.

Pin the direct CI tools in `requirements/ci.txt`. Build an sdist and wheel,
then install only the wheel into a fresh
environment outside the checkout and verify imports, CLI help, packaged schema
resources, tests, and `pip check`. Validate the package-contract and versioned
Draft 2020-12 schemas, then invoke the runtime parsers for semantic parity.
CodeQL runs the complete analysis and archives SARIF for review; enabling GitHub
Code Scanning is a separate repository capability. Schemas remain structural;
runtime loaders enforce bounded paths, placeholders, cross-file references,
and other semantic rules.

The existing branch ruleset receives one additive required status context named
`Required checks` only after implementation and review evidence is complete.

## Scope

In scope are repository CI, package/schema validation, contributor commands,
immutable Action provenance, Dependabot policy, CodeQL, and the additive
`Required checks` branch-policy rule. Runtime review semantics, provider
transport, credentials, release publication, and issue state remain out of
scope.

## Alternatives considered

1. Keep a mutable two-platform workflow: rejected because it leaves supported
   runtimes and Action provenance unverified.
2. Make every matrix entry run every expensive gate: rejected because it
   multiplies package, schema, coverage, and CodeQL cost without increasing
   coverage of the independent gates.
3. Publish a package or require provider credentials in CI: rejected because
   issue #3 is a pre-merge quality/supply-chain gate and runtime behavior must
   remain credential-free.

## Rollback

Before applying the branch-policy change, retain the full ruleset response for
ruleset `20269572` and the exact additive payload. If publication is abandoned,
restore the captured original `conditions`, `bypass_actors`, and `rules` array
with the rollback payload, then read it back and compare every field. Code
rollback is a revert of the issue #3 change before merge (or a normal revert
afterward). Dependency and schema rollback revert their declarations together;
runtime parsing remains unchanged.

## Validation

The local positive gates include pinned-Action inspection, Draft 2020-12
schema/runtime validation, Ruff, mypy, compileall, 81.21% branch coverage,
clean-wheel smoke/tests, `pip check`, YAML parsing, and `git diff --check`.
Negative coverage and invalid-schema probes fail closed as intended. Hosted
matrix, CodeQL artifact review, and ruleset readback remain required delivery
evidence; GitHub Code Scanning upload remains unavailable until the repository
capability is enabled.

## Rollout

Publish one draft PR based on the refreshed `origin/main`, then apply the
captured additive ruleset update only after outcome approval and readiness.
No package release or deployment is part of this rollout.

## Follow-up

When fallback mode is enabled, read back hosted Windows/macOS and Python
3.12–3.14 results, the CodeQL SARIF artifact, and the `Required checks` ruleset
after publication; keep the existing issue #7 ADR 0007 unchanged.

## Consequences

Contributors get reproducible local commands and a branch-coverage floor, while
the stable aggregate gives branch protection one durable contract. In Ubicloud
mode, Linux compatibility and the quality gates run on the same hosted runner;
Windows/macOS behavior is intentionally deferred until fallback mode is
enabled. CodeQL artifact review and ruleset enforcement still require hosted
evidence; local gates do not substitute for those checks. GitHub Code Scanning
upload remains a documented follow-up once the repository is eligible for that
feature.
