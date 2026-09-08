# ADR 0008 - Versioned public schemas and compatibility guarantees

Status: Proposed
Date: 2026-08-11
GitHub Issue: #10
Pull Request: not configured
Owners/Reviewers: Maintainers
Approved by: not configured

## Context

Code Sensei exposes public Python objects, CLI output, configuration files, and
provider-facing contracts. External integrators need machine-readable schemas,
stable error categories, and documented compatibility rules before they can
safely depend on the package.

## Decision Drivers

- Public JSON documents must be versioned and machine-validated.
- Existing v1 documents should not require a breaking `schema_version` field.
- Consumers need to distinguish input, format, provider, and transient
  failures without parsing prose.
- The review core must remain independent of GitHub and model-provider SDKs.

## Decision

Publish versioned JSON Schemas under `src/code_sensei/schemas/` with `$id`
values containing `/v1/`. Keep v1 documents without an explicit
`schema_version` field; schema identity is the `$id`. Additive changes are
allowed within v1, breaking changes require a v2 schema and new `$id`.

Add a small `src/code_sensei/schemas.py` loader/validator and a
`scripts/validate_public_schemas.py` script that validates packaged defaults,
examples, and golden fixtures. Add `jsonschema` as a runtime dependency.

Add stable `error_category` values to `CodeSenseiError` subclasses and a
`transient` flag to `ProviderError` for network/timeout failures. Document
public Python imports, provider protocol, CLI flags, exit codes, environment
variables, error categories, versioning, migration, and rollback in
`docs/public-contracts.md`.

## Scope

In scope:

- Versioned schemas for review results, learning entries/proposals, review
  categories, stage configuration, and concurrency plans.
- Schema validation for packaged defaults, examples, and golden fixtures.
- Stable error categories and provider transient metadata.
- Public contract documentation and ADR status reconciliation.

Out of scope:

- Changing existing review result or learning JSON field names.
- Adding `schema_version` fields to emitted v1 documents.
- Implementing a hosted schema registry or network service.

## Consequences

Positive:

- Integrators can validate emitted documents and configuration locally.
- Breaking changes are detectable through schema `$id` changes and golden
  fixtures.
- Error handling becomes machine-readable.

Negative or tradeoffs:

- The package gains a runtime dependency on `jsonschema`.
- Future schema changes must follow the documented compatibility policy.

## Alternatives Considered

### Add schema validation only in CI

- Summary: Validate examples in CI without a package module.
- Why not chosen: Integrators need a stable way to validate emitted documents.

### Add `schema_version` to emitted v1 documents

- Summary: Add an explicit version field to every document.
- Why not chosen: It would be a breaking change to existing output and is not
  needed when `$id` identifies the schema.

## Implementation Notes

- Schema files live under `src/code_sensei/schemas/` and are packaged as data.
- `validate_public_schemas.py` runs from the repository root after package
  install.
- CI runs the schema validation script after install.

## Validation And Rollout

- Validation: Unit tests cover schema identity, packaged defaults, examples,
  golden fixtures, negative fixtures, error categories, and transient provider
  failures.
- Rollout: Release notes and `docs/public-contracts.md` publish the
  compatibility policy before external integrators depend on it.
- Rollback: Revert the schema files, validator, dependency, error metadata,
  and documentation. No stored data migration is required.

## Follow-Up

- Future provider and publisher adapters must validate their public JSON
  documents against the published schemas.

## Links

- Related issue: #10
- Related PR: not configured
- Supersedes: none
- Superseded by: none
